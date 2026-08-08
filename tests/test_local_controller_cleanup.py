import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs")))

from ventis.controller.local_controller_frontend import LocalControllerServicer
import local_controler_pb2


class _SyncThread:
    """Stand-in for threading.Thread that runs its target immediately, inline.

    Cleanup() fires off a daemon Thread and returns without waiting for it, which
    makes the real dispatch nondeterministic to assert on in a test. Swapping this
    in for the module's Thread makes the dispatch happen synchronously instead.
    """

    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class CleanupDispatchTests(unittest.TestCase):
    def test_batched_request_ids_dispatches_cleanup_for_each(self):
        cleaned = []
        servicer = SimpleNamespace(_cleanup_request=lambda rid: cleaned.append(rid))
        request = local_controler_pb2.JsonResponse(
            resonse=json.dumps({"request_ids": ["req1", "req2", "req3"]})
        )

        with patch("ventis.controller.local_controller_frontend.Thread", _SyncThread):
            LocalControllerServicer.Cleanup(servicer, request, context=None)

        self.assertEqual(cleaned, ["req1", "req2", "req3"])

    def test_missing_ids_does_not_dispatch(self):
        cleaned = []
        servicer = SimpleNamespace(_cleanup_request=lambda rid: cleaned.append(rid))
        request = local_controler_pb2.JsonResponse(resonse=json.dumps({}))

        with patch("ventis.controller.local_controller_frontend.Thread", _SyncThread):
            LocalControllerServicer.Cleanup(servicer, request, context=None)

        self.assertEqual(cleaned, [])

    def test_old_single_request_id_payload_is_no_longer_supported(self):
        # The only real caller (GlobalController._trigger_cleanup) always sends
        # request_ids now, so the pre-batching {"request_id": ...} shape is
        # intentionally treated the same as a missing/empty payload, not dispatched.
        cleaned = []
        servicer = SimpleNamespace(_cleanup_request=lambda rid: cleaned.append(rid))
        request = local_controler_pb2.JsonResponse(
            resonse=json.dumps({"request_id": "req-legacy"})
        )

        with patch("ventis.controller.local_controller_frontend.Thread", _SyncThread):
            LocalControllerServicer.Cleanup(servicer, request, context=None)

        self.assertEqual(cleaned, [])


class _FakeRedisStore:
    """Enough of RedisClient's surface for _cleanup_request: strings, sets, setnx."""

    def __init__(self, strings=None, sets=None):
        self.strings = strings or {}
        self.sets = sets or {}

    def setnx(self, key, value):
        if key in self.strings:
            return False
        self.strings[key] = value
        return True

    def smembers(self, name):
        return set(self.sets.get(name, set()))

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.sets.pop(key, None)


def _bare_servicer(redis):
    servicer = LocalControllerServicer.__new__(LocalControllerServicer)
    servicer.redis = redis
    servicer.my_endpoint = "test-node"
    return servicer


class CleanupRequestPreservesMetricsTests(unittest.TestCase):
    """Bug F: cleanup must never delete future:{fid}:metrics itself -- that key is telemetry
    the GlobalController's poller still needs to read and persist. Deleting it here (on
    cleanup_interval, a timer independent of the poller's own poll_interval) could delete a
    future's only copy of its data before the poller ever ran, permanently losing it with no
    error anywhere. Confirmed live: a ~4s request's futures got cleaned up before the next 5s
    poll tick read them."""

    def test_cleanup_deletes_future_bookkeeping_but_not_its_metrics(self):
        redis = _FakeRedisStore(
            sets={"request:req1:futures": {"fut1", "fut2"}},
            strings={
                "future:fut1:metrics": "irrelevant-value",
                "future:fut2:metrics": "irrelevant-value",
                "future:fut1": "x",
                "future:fut1:children": "x",
                "future:fut1:consumers": "x",
            },
        )
        servicer = _bare_servicer(redis)

        servicer._cleanup_request("req1")

        # Future-resolution bookkeeping: gone.
        self.assertNotIn("request:req1:futures", redis.sets)
        self.assertNotIn("future:fut1", redis.strings)
        self.assertNotIn("future:fut1:children", redis.strings)
        self.assertNotIn("future:fut1:consumers", redis.strings)
        # Metrics: untouched -- only the poller may delete these, after a confirmed write.
        self.assertIn("future:fut1:metrics", redis.strings)
        self.assertIn("future:fut2:metrics", redis.strings)

    def test_cleanup_still_deletes_affinity_bindings(self):
        redis = _FakeRedisStore(
            sets={"request:req1:futures": {"fut1"}},
            strings={"future:fut1:metrics": "x", "affinity:req1": "some-host"},
        )
        servicer = _bare_servicer(redis)

        servicer._cleanup_request("req1")

        self.assertNotIn("affinity:req1", redis.strings)
        self.assertIn("future:fut1:metrics", redis.strings)

    def test_cleanup_releases_its_lock_even_with_no_futures(self):
        redis = _FakeRedisStore(sets={"request:req1:futures": set()})
        servicer = _bare_servicer(redis)

        servicer._cleanup_request("req1")

        self.assertNotIn("request:req1:cleanup_lock", redis.strings)


if __name__ == "__main__":
    unittest.main()
