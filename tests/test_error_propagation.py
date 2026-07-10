import os
import sys
import unittest
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
from ventis.future import Future


class _FakeRedis:
    def __init__(self):
        self.hashes = {}

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)


class ErrorPropagationTests(unittest.TestCase):
    def test_forward_request_writes_future_error_on_grpc_failure(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(Execute=MagicMock(side_effect=RuntimeError("boom")))
        controller = SimpleNamespace(
            redis=redis,
            _my_endpoint="172.31.19.107:50051",
            _get_remote_stub=lambda endpoint: stub,
        )
        data = {
            "future_id": "future-1",
            "service": "ExampleAgent",
            "function": "hello",
        }

        LocalController._forward_request(controller, "172.31.23.135:50051", data)

        self.assertEqual(data["origin"], "172.31.19.107:50051")
        self.assertEqual(redis.hget("future:future-1", "error"), "boom")
        stub.Execute.assert_called_once()

    def test_future_poll_redis_raises_runtime_error_when_error_is_present(self):
        redis = _FakeRedis()
        redis.hset("future:future-1", "error", "boom")
        future = SimpleNamespace(redis=redis, _key=lambda: "future:future-1")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            Future._poll_redis(future)

    def test_future_poll_redis_returns_result_when_error_is_absent(self):
        redis = _FakeRedis()
        redis.hset("future:future-1", "result", "Hello, World!")
        future = SimpleNamespace(
            redis=redis, _key=lambda: "future:future-1", result=None
        )

        result = Future._poll_redis(future)

        self.assertEqual(result, "Hello, World!")
        self.assertEqual(future.result, "Hello, World!")


if __name__ == "__main__":
    unittest.main()
