import os
import sys
import unittest
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "ventis", "templates", "grpc_stubs"
        )
    ),
)

from ventis.controller.local_controller import LocalController
from ventis.controller.local_controller_frontend import LocalControllerServicer
from ventis.future import Future
import local_controler_pb2


class _FakeRedis:
    def __init__(self):
        self.hashes = {}

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hincrby(self, name, field, amount=1):
        bucket = self.hashes.setdefault(name, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return bucket[field]


def _bind_failure_marker(controller):
    controller._mark_future_failed = lambda future_id, error, origin=None, error_name=None: (
        LocalController._mark_future_failed(
            controller, future_id, error, origin, error_name
        )
    )
    return controller


class ErrorPropagationTests(unittest.TestCase):
    def test_forward_request_writes_future_error_on_grpc_failure(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(Execute=MagicMock(side_effect=RuntimeError("boom")))
        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
            agent_id=None,
            agent_name=None,
            _my_endpoint="172.31.19.107:50051",
            _get_remote_stub=lambda endpoint: stub,
        ))
        data = {
            "future_id": "future-1",
            "service": "ExampleAgent",
            "function": "hello",
        }

        LocalController._forward_request(controller, "172.31.23.135:50051", data)

        self.assertEqual(data["origin"], "172.31.19.107:50051")
        self.assertEqual(redis.hget("future:future-1", "error"), "RuntimeError")
        logs = json.loads(redis.hget("future:future-1", "logs"))
        self.assertEqual(logs[0]["Body"], "boom")
        stub.Execute.assert_called_once()

    def test_future_value_raises_runtime_error_when_error_is_present(self):
        redis = _FakeRedis()
        redis.hset("future:future-1", "error", "boom")
        future = SimpleNamespace(
            redis=redis,
            _key=lambda: "future:future-1",
            _poll_redis=lambda: Future._poll_redis(future),
            result=None,
        )

        with self.assertRaisesRegex(RuntimeError, "boom"):
            Future.value(future)

    def test_future_poll_redis_returns_result_when_error_is_absent(self):
        redis = _FakeRedis()
        redis.hset("future:future-1", "result", "Hello, World!")
        future = SimpleNamespace(
            redis=redis,
            _key=lambda: "future:future-1",
            _poll_redis=lambda: Future._poll_redis(future),
            id="future-1",
            result=None,
        )

        result = Future._poll_redis(future)

        self.assertEqual(result, "Hello, World!")
        self.assertEqual(future.result, "Hello, World!")

    def test_future_value_raises_when_metrics_mark_it_failed(self):
        redis = _FakeRedis()
        redis.hset_multiple(
            "future:future-1",
            {"failed": 1, "error": "agent exploded"},
        )
        future = SimpleNamespace(
            redis=redis,
            _key=lambda: "future:future-1",
            _poll_redis=lambda: Future._poll_redis(future),
            id="future-1",
            result=None,
        )

        with self.assertRaisesRegex(RuntimeError, "agent exploded"):
            Future.value(future)

    def test_result_callback_sends_error_separately_from_result(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(WriteResult=MagicMock())
        controller = SimpleNamespace(
            redis=redis,
            agent_name="ExampleAgent",
            _get_remote_stub=lambda endpoint: stub,
        )

        LocalController._send_result_callback(
            controller,
            "origin:50051",
            "future-1",
            failed=1,
            error_message="agent exploded",
        )

        payload = stub.WriteResult.call_args.args[0].resonse
        self.assertEqual(
            json.loads(payload),
            {
                "future_id": "future-1",
                "result": "",
                "failed": 1,
                "error": "agent exploded",
            },
        )

    def test_write_result_persists_remote_error_as_terminal_failure(self):
        redis = _FakeRedis()
        servicer = SimpleNamespace(redis=redis)
        request = local_controler_pb2.JsonResponse(
            resonse=json.dumps(
                {
                    "future_id": "future-1",
                    "failed": 1,
                    "error": "remote exploded",
                }
            )
        )
        context = SimpleNamespace(peer=lambda: "peer:50051")

        LocalControllerServicer.WriteResult(servicer, request, context)

        self.assertEqual(
            redis.hget("future:future-1", "failed"), 1
        )
        self.assertEqual(
            redis.hget("future:future-1", "error"),
            "remote exploded",
        )

    def test_malformed_request_with_future_id_is_marked_failed(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(
            SimpleNamespace(
                redis=redis, agent_id=None, agent_name=None, _my_endpoint="localhost:50051"
            )
        )

        LocalController._process_request(controller, {"future_id": "future-1"})

        self.assertEqual(redis.hget("future:future-1", "error"), "MalformedRequest")
        self.assertEqual(redis.hget("future:future-1", "failed"), 1)
        logs = json.loads(redis.hget("future:future-1", "logs"))
        self.assertEqual(
            logs[0]["Body"],
            "Malformed request: missing service, function, or future_id",
        )
        self.assertEqual(logs[0]["Attributes"]["exception.type"], "MalformedRequest")

    def test_policy_denied_request_is_marked_failed_with_category(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(
            SimpleNamespace(
                redis=redis, agent_id=None, agent_name=None, _my_endpoint="localhost:50051",
            )
        )
        controller._load_policy_rules = lambda: [
            {"match": {"role": "admin"}, "access": ["SomeAgent"]}
        ]
        controller._check_policy = lambda service, context: LocalController._check_policy(
            controller, service, context
        )

        LocalController._process_request(
            controller,
            {"service": "SomeAgent", "function": "do", "future_id": "future-2"},
        )

        self.assertEqual(redis.hget("future:future-2", "error"), "PolicyDenied")
        logs = json.loads(redis.hget("future:future-2", "logs"))
        self.assertEqual(logs[0]["Attributes"]["exception.type"], "PolicyDenied")
        self.assertIn("Policy denied", logs[0]["Body"])

    def test_no_endpoint_found_is_marked_failed_with_category(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(
            SimpleNamespace(
                redis=redis, agent_id=None, agent_name=None, _my_endpoint="localhost:50051",
            )
        )
        controller._load_policy_rules = lambda: []
        controller._check_policy = lambda service, context: LocalController._check_policy(
            controller, service, context
        )
        controller._resolve_endpoint = lambda service, request_id: None

        LocalController._process_request(
            controller,
            {"service": "SomeAgent", "function": "do", "future_id": "future-3"},
        )

        self.assertEqual(redis.hget("future:future-3", "error"), "NoEndpointFound")
        logs = json.loads(redis.hget("future:future-3", "logs"))
        self.assertEqual(logs[0]["Attributes"]["exception.type"], "NoEndpointFound")

    def test_unknown_method_is_marked_failed_with_category(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(
            SimpleNamespace(
                redis=redis,
                agent=SimpleNamespace(),
                agent_name="ExampleAgent",
                agent_id="agent-1",
                _my_endpoint="localhost:50051",
                _metrics_key="controller:localhost:50051:metrics",
            )
        )

        LocalController._execute_locally(
            controller, "ExampleAgent", "missing_method", {}, "future-4"
        )

        self.assertEqual(redis.hget("future:future-4", "error"), "UnknownMethod")
        logs = json.loads(redis.hget("future:future-4", "logs"))
        self.assertEqual(logs[0]["Attributes"]["exception.type"], "UnknownMethod")

    def test_result_callback_failure_is_marked_with_category(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(WriteResult=MagicMock(side_effect=RuntimeError("boom")))
        controller = _bind_failure_marker(
            SimpleNamespace(
                redis=redis,
                agent_id="agent-1",
                agent_name="ExampleAgent",
                _my_endpoint="localhost:50051",
                _get_remote_stub=lambda endpoint: stub,
            )
        )

        LocalController._send_result_callback(
            controller,
            "origin:50051",
            "future-5",
            failed=1,
            error_message="agent exploded",
        )

        self.assertEqual(redis.hget("future:future-5", "error"), "ResultCallbackFailed")
        logs = json.loads(redis.hget("future:future-5", "logs"))
        self.assertEqual(logs[0]["Attributes"]["exception.type"], "ResultCallbackFailed")
        self.assertIn("Result callback failed", logs[0]["Body"])

    def test_cross_instance_failure_snapshot_merges_into_origin_and_raises(self):
        """Simulate origin and executor on separate Redis instances: the
        executor's completion callback must carry the full execution snapshot
        so Future.value() on the origin raises the original error_message."""
        origin_redis = _FakeRedis()
        executor_redis = _FakeRedis()

        origin_redis.hset_multiple(
            "future:future-1",
            {"id": "future-1", "service": "Greeter", "method": "greet", "result": ""},
        )

        def boom():
            raise ValueError("executor exploded")

        stub = SimpleNamespace(WriteResult=MagicMock())
        callback_payloads = []

        def capture_write_result(request):
            callback_payloads.append(request.resonse)

        stub.WriteResult.side_effect = capture_write_result

        executor = SimpleNamespace(
            redis=executor_redis,
            agent=SimpleNamespace(greet=boom),
            agent_name="Greeter",
            agent_id="executor-agent",
            _my_endpoint="executor:50051",
            _metrics_key="controller:executor:50051:metrics",
            _resolve_future_args=lambda args: args,
            _get_remote_stub=lambda endpoint: stub,
        )
        executor._mark_future_failed = lambda future_id, error, origin=None: (
            LocalController._mark_future_failed(executor, future_id, error, origin)
        )
        executor._send_result_callback = lambda *a, **k: (
            LocalController._send_result_callback(executor, *a, **k)
        )

        LocalController._execute_locally(
            executor, "Greeter", "greet", {}, "future-1", origin="origin:50051"
        )

        # Feed the captured callback into the origin's WriteResult receiver.
        origin_servicer = SimpleNamespace(redis=origin_redis)
        for payload in callback_payloads:
            request = local_controler_pb2.JsonResponse(resonse=payload)
            context = SimpleNamespace(peer=lambda: "executor:50051")
            LocalControllerServicer.WriteResult(origin_servicer, request, context)

        self.assertEqual(origin_redis.hget("future:future-1", "failed"), 1)
        self.assertEqual(origin_redis.hget("future:future-1", "error"), "ValueError")
        self.assertIn("cpu_resource", origin_redis.hashes["future:future-1"])
        self.assertIn("finished_at", origin_redis.hashes["future:future-1"])
        self.assertEqual(origin_redis.hget("future:future-1", "agent"), "executor-agent")

        # The full detail -- the whole point of this feature -- now crosses
        # the instance boundary too, not just the type name, riding along in
        # the same hash snapshot as every other field.
        origin_logs = json.loads(origin_redis.hget("future:future-1", "logs"))
        origin_entry = origin_logs[0]
        self.assertEqual(origin_entry["Attributes"]["exception.type"], "ValueError")
        self.assertEqual(origin_entry["Body"], "executor exploded")
        self.assertEqual(origin_entry["Attributes"]["agent.id"], "executor-agent")
        self.assertEqual(origin_entry["Attributes"]["agent.name"], "Greeter")
        self.assertEqual(origin_entry["Attributes"]["endpoint"], "executor:50051")
        self.assertIsNotNone(origin_entry["Attributes"]["exception.stacktrace"])

        origin_future = SimpleNamespace(
            redis=origin_redis,
            _key=lambda: "future:future-1",
            _poll_redis=lambda: Future._poll_redis(origin_future),
            id="future-1",
            result=None,
        )
        with self.assertRaisesRegex(RuntimeError, "ValueError"):
            Future.value(origin_future)

        # Executor's own local copy is untouched by the origin-side merge.
        self.assertEqual(executor_redis.hget("future:future-1", "error"), "ValueError")

    def test_same_host_relay_does_not_duplicate_logs(self):
        """Two replicas sharing one Redis (the default same-host topology) must
        not double-record a failure: the executor writes `logs` directly,
        then relays to origin's WriteResult against that same Redis instance --
        hset overwrites idempotently instead of appending a second entry."""
        shared_redis = _FakeRedis()

        def boom():
            raise ValueError("shared exploded")

        stub = SimpleNamespace(WriteResult=MagicMock())
        callback_payloads = []
        stub.WriteResult.side_effect = lambda request: callback_payloads.append(
            request.resonse
        )

        executor = SimpleNamespace(
            redis=shared_redis,
            agent=SimpleNamespace(greet=boom),
            agent_name="Greeter",
            agent_id="replica-a",
            _my_endpoint="localhost:8001",
            _metrics_key="controller:localhost:8001:metrics",
            _resolve_future_args=lambda args: args,
            _get_remote_stub=lambda endpoint: stub,
        )
        executor._mark_future_failed = lambda future_id, error, origin=None: (
            LocalController._mark_future_failed(executor, future_id, error, origin)
        )
        executor._send_result_callback = lambda *a, **k: (
            LocalController._send_result_callback(executor, *a, **k)
        )

        LocalController._execute_locally(
            executor, "Greeter", "greet", {}, "future-1", origin="localhost:8002"
        )

        origin_servicer = SimpleNamespace(redis=shared_redis)
        for payload in callback_payloads:
            request = local_controler_pb2.JsonResponse(resonse=payload)
            context = SimpleNamespace(peer=lambda: "localhost:8001")
            LocalControllerServicer.WriteResult(origin_servicer, request, context)

        logs = json.loads(shared_redis.hget("future:future-1", "logs"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["Body"], "shared exploded")

    def test_mark_future_failed_never_raises_when_redis_is_unreachable(self):
        """A Redis blip while recording a failure must not escape _mark_future_failed --
        callers (including the main polling loop) rely on this being a safe sink."""
        class _FlakyRedis(_FakeRedis):
            def hset_multiple(self, name, mapping):
                raise ConnectionError("redis unreachable")

        controller = _bind_failure_marker(SimpleNamespace(
            redis=_FlakyRedis(),
            agent_id=None,
            agent_name=None,
            _my_endpoint="localhost:50051",
        ))

        try:
            controller._mark_future_failed("future-1", ValueError("boom"))
        except Exception as e:
            self.fail(f"_mark_future_failed raised unexpectedly: {e}")


if __name__ == "__main__":
    unittest.main()
