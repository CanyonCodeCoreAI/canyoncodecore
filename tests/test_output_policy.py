"""Unit tests for the agent output-policy mechanism (issues/003).

Self-contained: no Docker / Redis / gRPC required. We stub the generated
gRPC modules so the controller module imports, inject fake policy modules
via sys.modules, and exercise the real
``LocalController._load_output_policies`` / ``_run_output_policies`` against
a fake Redis. Run: ``uv run python tests/test_output_policy.py``.

Policies are side-effect only: each receives the same agent output, their
return value is ignored, and they never modify the result.
"""

import json
import os
import sys
import types

# Make the repo root importable so `import ventis...` works when run directly.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# --- Stub the generated gRPC modules the controller imports at module load. ---
_pb2 = types.ModuleType("local_controler_pb2")


class _JsonResponse:
    def __init__(self, resonse=""):
        self.resonse = resonse


_pb2.JsonResponse = _JsonResponse
sys.modules["local_controler_pb2"] = _pb2

_pb2_grpc = types.ModuleType("local_controler_pb2_grpc")


class _Servicer:  # used as a base class at import time by the frontend
    pass


class _Stub:
    def __init__(self, channel):
        pass


_pb2_grpc.LocalControllerServicer = _Servicer
_pb2_grpc.LocalControllerStub = _Stub
_pb2_grpc.add_LocalControllerServicer_to_server = lambda servicer, server: None
sys.modules["local_controler_pb2_grpc"] = _pb2_grpc

from ventis.controller.local_controller import LocalController, OUTPUT_POLICY_KEY_FMT  # noqa: E402


# --- Fake Redis: only .get is used by _load_output_policies. ---
class FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    def get(self, key):
        return self.store.get(key)


# --- Fake policy module, resolved via importlib "module:function". ---
_seen = []   # (tag, output, request_id) recorded by side-effect policies


def _record_a(output, ctx):
    _seen.append(("a", output, ctx.get("request_id")))


def _record_b(output, ctx):
    _seen.append(("b", output, ctx.get("request_id")))


def _tries_to_transform(output, ctx):
    # Returns a value on purpose; the framework must IGNORE it.
    _seen.append(("t", output, ctx.get("request_id")))
    return "MUTATED"


def _boom(output, ctx):
    raise RuntimeError("intentional policy failure")


_polmod = types.ModuleType("fake_policies")
_polmod.record_a = _record_a
_polmod.record_b = _record_b
_polmod.tries_to_transform = _tries_to_transform
_polmod.boom = _boom
sys.modules["fake_policies"] = _polmod


def _controller_with(refs):
    """Build a bare controller wired to a fake Redis holding `refs` for 'Svc'."""
    lc = LocalController.__new__(LocalController)
    lc._output_policies = {}
    store = {}
    if refs is not None:
        store[OUTPUT_POLICY_KEY_FMT.format(service="Svc")] = json.dumps(refs)
    lc.redis = FakeRedis(store)
    return lc


def test_all_policies_run_on_same_output():
    _seen.clear()
    lc = _controller_with(["fake_policies:record_a", "fake_policies:record_b"])
    ret = lc._run_output_policies("Svc", "hello", {"request_id": "r1"})
    # Side-effect only: nothing is returned...
    assert ret is None, ret
    # ...and every policy saw the SAME original output.
    assert _seen == [("a", "hello", "r1"), ("b", "hello", "r1")], _seen


def test_return_value_is_ignored():
    _seen.clear()
    lc = _controller_with(["fake_policies:tries_to_transform", "fake_policies:record_b"])
    lc._run_output_policies("Svc", "orig", {"request_id": "r2"})
    # The transform policy ran but its "MUTATED" return is dropped: record_b
    # still receives the original output, not "MUTATED".
    assert _seen == [("t", "orig", "r2"), ("b", "orig", "r2")], _seen


def test_no_policies_configured_is_noop():
    _seen.clear()
    lc = _controller_with(None)  # nothing in Redis
    assert lc._run_output_policies("Svc", "x", {}) is None
    assert _seen == []


def test_exception_is_isolated():
    _seen.clear()
    lc = _controller_with(["fake_policies:boom", "fake_policies:record_b"])
    lc._run_output_policies("Svc", "hi", {"request_id": "r3"})
    # boom raises and is skipped; record_b still runs.
    assert _seen == [("b", "hi", "r3")], _seen


def test_unresolvable_reference_is_skipped():
    _seen.clear()
    lc = _controller_with(["no_such_module:nope", "fake_policies:record_a"])
    lc._run_output_policies("Svc", "hi", {"request_id": "r4"})
    assert _seen == [("a", "hi", "r4")], _seen


def test_order_does_not_affect_result():
    # Both orderings run both policies; result (the passed-in output) is never
    # changed regardless of order.
    _seen.clear()
    lc1 = _controller_with(["fake_policies:record_a", "fake_policies:record_b"])
    lc1._run_output_policies("Svc", "same", {})
    forward = list(_seen)
    _seen.clear()
    lc2 = _controller_with(["fake_policies:record_b", "fake_policies:record_a"])
    lc2._run_output_policies("Svc", "same", {})
    reverse = list(_seen)
    # Different call order, but every policy saw the identical unchanged output.
    assert {o for _, o, _ in forward} == {"same"}
    assert {o for _, o, _ in reverse} == {"same"}


def test_resolution_is_cached():
    _seen.clear()
    lc = _controller_with(["fake_policies:record_a"])
    lc._run_output_policies("Svc", "a", {})
    # Mutate the backing store; cache should mean it's not re-read.
    lc.redis.store[OUTPUT_POLICY_KEY_FMT.format(service="Svc")] = json.dumps([])
    lc._run_output_policies("Svc", "b", {})
    assert [t for t, _, _ in _seen] == ["a", "a"], _seen


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 43)
    if failed:
        print(f"{failed}/{len(tests)} test(s) failed.")
        sys.exit(1)
    print(f"All {len(tests)} output-policy tests passed.")
