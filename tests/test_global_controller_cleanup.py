import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs")))

from canyonos_core.controller.global_controller import GlobalController
import local_controler_pb2


class _FakeRedis:
    def __init__(self, sets=None, hashes=None):
        self.sets = sets or {}
        self.hashes = hashes or {}

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)

    def srem(self, name, *values):
        self.sets.get(name, set()).difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(name, set()))

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))


class _FakeStub:
    def __init__(self):
        self.calls = []

    def Cleanup(self, request):
        self.calls.append(json.loads(request.resonse))
        return local_controler_pb2.JsonResponse(resonse="Cleanup triggered")


class _SpyRedis(_FakeRedis):
    """_FakeRedis that also records every srem call, so a test can assert
    exactly which client a drain happened against (and that a client with
    nothing completed is never touched at all)."""

    def __init__(self, sets=None):
        super().__init__(sets)
        self.srem_calls = []

    def srem(self, name, *values):
        self.srem_calls.append((name, values))
        super().srem(name, *values)


class _FailingStub:
    def Cleanup(self, request):
        raise RuntimeError("connection refused")


class _FakeInstanceManager:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self):
        return self._instances


def _bare_controller(redis, instances, node_redis=None):
    """Build a GlobalController without running its heavy __init__.

    `node_redis` optionally simulates the host -> RedisClient map that
    _launch_redis_containers()/EC2 bootstrap populate. Passing None (the
    default) leaves the attribute unset entirely, reproducing a controller
    that never got node_redis set at all -- _trigger_cleanup must tolerate
    this and fall back to `redis` alone.
    """
    controller = GlobalController.__new__(GlobalController)
    controller.redis = redis
    controller.instance_manager = _FakeInstanceManager(instances)
    controller._lc_stubs = {}
    if node_redis is not None:
        controller.node_redis = node_redis
    return controller


class TriggerCleanupTests(unittest.TestCase):
    def test_sends_one_batched_call_per_instance_not_per_request(self):
        # A backlog of many completed requests must not multiply the number of
        # gRPC calls per instance -- one call per instance carrying the whole
        # batch, regardless of how large the backlog is.
        completed = {f"req{i}" for i in range(25)}
        expected = set(completed)  # snapshot -- _FakeRedis aliases this set, and
        # _trigger_cleanup drains "request:completed" via srem in place.
        redis = _FakeRedis({"request:completed": completed})
        instances = [{"endpoint": f"host{i}:50051"} for i in range(3)]
        controller = _bare_controller(redis, instances)

        stubs = {instance["endpoint"]: _FakeStub() for instance in instances}
        controller._get_lc_stub = lambda endpoint: stubs[endpoint]

        controller._trigger_cleanup()

        for endpoint, stub in stubs.items():
            self.assertEqual(
                len(stub.calls), 1, f"expected exactly one Cleanup call to {endpoint}"
            )
            self.assertEqual(set(stub.calls[0]["request_ids"]), expected)

        # Drained after broadcasting, same as before.
        self.assertEqual(redis.smembers("request:completed"), set())

    def test_one_instance_failing_does_not_block_others_or_stop_draining(self):
        completed = {"reqA", "reqB"}
        expected = set(completed)  # snapshot -- see note in the test above
        redis = _FakeRedis({"request:completed": completed})
        instances = [{"endpoint": "good:50051"}, {"endpoint": "bad:50051"}]
        controller = _bare_controller(redis, instances)

        good_stub = _FakeStub()
        stubs = {"good:50051": good_stub, "bad:50051": _FailingStub()}
        controller._get_lc_stub = lambda endpoint: stubs[endpoint]

        controller._trigger_cleanup()  # must not raise

        self.assertEqual(len(good_stub.calls), 1)
        self.assertEqual(set(good_stub.calls[0]["request_ids"]), expected)
        self.assertEqual(redis.smembers("request:completed"), set())

    def test_noop_when_nothing_completed(self):
        redis = _FakeRedis()
        instances = [{"endpoint": "host0:50051"}]
        controller = _bare_controller(redis, instances)

        stub = _FakeStub()
        controller._get_lc_stub = lambda endpoint: stub

        controller._trigger_cleanup()
        self.assertEqual(stub.calls, [])

    def test_noop_when_no_instances_registered(self):
        redis = _FakeRedis({"request:completed": {"req1"}})
        controller = _bare_controller(redis, [])

        # Should still drain the completed set even with nothing to broadcast to.
        controller._trigger_cleanup()
        self.assertEqual(redis.smembers("request:completed"), set())


class MultiNodeTriggerCleanupTests(unittest.TestCase):
    """Bug D: _trigger_cleanup must not be blind to non-localhost node Redis
    instances -- each replica (local or EC2) records its own completions in
    its own Redis, never centrally. See CLEANUP_FIX.md."""

    def test_completed_request_only_on_non_localhost_node_gets_cleaned(self):
        localhost_redis = _FakeRedis()
        ec2_redis = _FakeRedis({"request:completed": {"reqE"}})
        node_redis = {"localhost": localhost_redis, "10.0.0.5": ec2_redis}
        controller = _bare_controller(
            localhost_redis, [{"endpoint": "wf:50051"}], node_redis=node_redis
        )

        stub = _FakeStub()
        controller._get_lc_stub = lambda endpoint: stub

        controller._trigger_cleanup()

        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(set(stub.calls[0]["request_ids"]), {"reqE"})
        self.assertEqual(ec2_redis.smembers("request:completed"), set())

    def test_requests_across_multiple_nodes_batched_into_one_call_per_instance(self):
        localhost_redis = _FakeRedis({"request:completed": {"reqA", "reqB"}})
        ec2_redis_1 = _FakeRedis({"request:completed": {"reqC"}})
        ec2_redis_2 = _FakeRedis({"request:completed": {"reqD", "reqE"}})
        node_redis = {
            "localhost": localhost_redis,
            "ec2-1": ec2_redis_1,
            "ec2-2": ec2_redis_2,
        }
        instances = [{"endpoint": f"host{i}:50051"} for i in range(3)]
        controller = _bare_controller(localhost_redis, instances, node_redis=node_redis)

        stubs = {instance["endpoint"]: _FakeStub() for instance in instances}
        controller._get_lc_stub = lambda endpoint: stubs[endpoint]

        controller._trigger_cleanup()

        expected = {"reqA", "reqB", "reqC", "reqD", "reqE"}
        for endpoint, stub in stubs.items():
            self.assertEqual(
                len(stub.calls), 1, f"expected exactly one Cleanup call to {endpoint}"
            )
            self.assertEqual(set(stub.calls[0]["request_ids"]), expected)

        for redis in (localhost_redis, ec2_redis_1, ec2_redis_2):
            self.assertEqual(redis.smembers("request:completed"), set())

    def test_each_node_drained_from_its_own_client_not_cross_contaminated(self):
        redis_with_data = _SpyRedis({"request:completed": {"reqX"}})
        redis_empty = _SpyRedis()
        node_redis = {"a": redis_with_data, "b": redis_empty}
        controller = _bare_controller(
            redis_with_data, [{"endpoint": "wf:50051"}], node_redis=node_redis
        )

        stub = _FakeStub()
        controller._get_lc_stub = lambda endpoint: stub

        controller._trigger_cleanup()

        self.assertEqual(redis_with_data.srem_calls, [("request:completed", ("reqX",))])
        self.assertEqual(redis_empty.srem_calls, [])

    def test_falls_back_to_self_redis_when_node_redis_attribute_missing(self):
        # No node_redis kwarg at all -- the attribute genuinely doesn't exist,
        # reproducing a controller built before _launch_redis_containers() ever
        # ran. Behavior must match the pre-Bug-D single-redis path exactly.
        completed = {"req1", "req2"}
        redis = _FakeRedis({"request:completed": set(completed)})
        controller = _bare_controller(redis, [{"endpoint": "host0:50051"}])
        self.assertFalse(hasattr(controller, "node_redis"))

        stub = _FakeStub()
        controller._get_lc_stub = lambda endpoint: stub

        controller._trigger_cleanup()

        self.assertEqual(set(stub.calls[0]["request_ids"]), completed)
        self.assertEqual(redis.smembers("request:completed"), set())

    def test_falls_back_to_self_redis_when_node_redis_is_empty_dict(self):
        # node_redis present but empty -- the window right at the start of
        # __init__, before _launch_redis_containers() populates it.
        completed = {"req1"}
        redis = _FakeRedis({"request:completed": set(completed)})
        controller = _bare_controller(
            redis, [{"endpoint": "host0:50051"}], node_redis={}
        )

        stub = _FakeStub()
        controller._get_lc_stub = lambda endpoint: stub

        controller._trigger_cleanup()

        self.assertEqual(set(stub.calls[0]["request_ids"]), completed)
        self.assertEqual(redis.smembers("request:completed"), set())


if __name__ == "__main__":
    unittest.main()
