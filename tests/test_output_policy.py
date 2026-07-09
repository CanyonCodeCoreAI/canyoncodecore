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


def test_policy_context_framework_fields_override_user_context():
    # The request context is caller-controlled (arrives via baggage). Framework
    # fields must be authoritative, so a spoofed service/request_id/function in
    # the user context cannot shadow the real ones handed to a policy.
    ctx = LocalController._policy_context(
        {"service": "SPOOF", "request_id": "evil", "function": "hack", "origin": "analyst"},
        request_id="real-req",
        service="FinanceAgent",
        function="run",
    )
    assert ctx["service"] == "FinanceAgent"
    assert ctx["request_id"] == "real-req"
    assert ctx["function"] == "run"
    # Non-framework keys from the request context are preserved.
    assert ctx["origin"] == "analyst"


def test_policy_context_handles_none_context():
    ctx = LocalController._policy_context(None, request_id="r", service="S", function="f")
    assert ctx == {"request_id": "r", "service": "S", "function": "f"}


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


def test_load_failure_is_isolated():
    # A malformed Redis value (or a Redis read error) during policy loading must
    # NOT propagate out of _run_output_policies — otherwise _execute_locally's
    # outer except would overwrite a successful agent result with an error.
    _seen.clear()
    lc = LocalController.__new__(LocalController)
    lc._output_policies = {}
    lc.redis = FakeRedis({OUTPUT_POLICY_KEY_FMT.format(service="Svc"): "{not json"})
    # Must return quietly rather than raise.
    assert lc._run_output_policies("Svc", "hi", {"request_id": "r5"}) is None
    assert _seen == []


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


# --- Real-module tests: exercise the actual examples/policies/finance.py ---
# through the real resolution + dispatch path (no fakes). This is what proves
# the module this PR ships is usable, not just the logic in isolation.
import logging  # noqa: E402

_EXAMPLES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples"))


def _capture(logger_name):
    """Attach a collecting handler to `logger_name`; return (records, detach)."""
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger(logger_name)
    h = _H()
    lg.addHandler(h)
    prev_level = lg.level
    lg.setLevel(logging.DEBUG)

    def detach():
        lg.removeHandler(h)
        lg.setLevel(prev_level)

    return records, detach


def _real_policy_controller():
    # Put examples/ on sys.path so `import policies.finance` resolves — the
    # same layout a local deploy runs in (project root == examples/).
    if _EXAMPLES_DIR not in sys.path:
        sys.path.insert(0, _EXAMPLES_DIR)
    lc = LocalController.__new__(LocalController)
    lc._output_policies = {}
    lc.redis = FakeRedis({
        OUTPUT_POLICY_KEY_FMT.format(service="FinanceAgent"): json.dumps([
            "policies.finance:audit_log",
            "policies.finance:alert_on_sensitive",
        ])
    })
    return lc


def test_real_example_policies_resolve_and_run():
    lc = _real_policy_controller()
    records, detach = _capture("policies.finance")
    try:
        ctx = {"request_id": "r9", "service": "FinanceAgent", "function": "run"}
        ret = lc._run_output_policies("FinanceAgent", "leaked SSN here", ctx)
    finally:
        detach()
    assert ret is None
    msgs = [r.getMessage() for r in records]
    # audit_log actually ran...
    assert any("[audit]" in m for m in msgs), msgs
    # ...and alert_on_sensitive fired on the SSN token.
    assert any("sensitive token" in m and "SSN" in m for m in msgs), msgs


def test_real_example_alert_quiet_when_clean():
    lc = _real_policy_controller()
    records, detach = _capture("policies.finance")
    try:
        ctx = {"request_id": "r10", "service": "FinanceAgent", "function": "run"}
        lc._run_output_policies("FinanceAgent", "nothing sensitive", ctx)
    finally:
        detach()
    msgs = [r.getMessage() for r in records]
    assert any("[audit]" in m for m in msgs), msgs          # audit still logs
    assert not any("sensitive token" in m for m in msgs), msgs  # no false alert


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
