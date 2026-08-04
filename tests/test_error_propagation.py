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


def _bind_failure_marker(controller):
    controller._mark_future_failed = lambda future_id, error, origin=None: (
        LocalController._mark_future_failed(controller, future_id, error, origin)
    )
    return controller


class ErrorPropagationTests(unittest.TestCase):
    def test_forward_request_writes_future_error_on_grpc_failure(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(Execute=MagicMock(side_effect=RuntimeError("boom")))
        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
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
        self.assertEqual(redis.hget("future:future-1", "error"), "boom")
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
            "future:future-1:metrics",
            {"failed": 1, "error_message": "agent exploded"},
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
                "result": None,
                "failed": 1,
                "error_message": "agent exploded",
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
                    "error_message": "remote exploded",
                }
            )
        )
        context = SimpleNamespace(peer=lambda: "peer:50051")

        LocalControllerServicer.WriteResult(servicer, request, context)

        self.assertEqual(
            redis.hget("future:future-1:metrics", "failed"), 1
        )
        self.assertEqual(
            redis.hget("future:future-1:metrics", "error_message"),
            "remote exploded",
        )

    def test_malformed_request_with_future_id_is_marked_failed(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(
            SimpleNamespace(redis=redis, _my_endpoint="localhost:50051")
        )

        LocalController._process_request(controller, {"future_id": "future-1"})

        self.assertEqual(
            redis.hget("future:future-1", "error"),
            "Malformed request: missing service, function, or future_id",
        )
        self.assertEqual(redis.hget("future:future-1:metrics", "failed"), 1)


if __name__ == "__main__":
    unittest.main()
