import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "canyonos_core", "templates", "grpc_stubs"
        )
    ),
)

class _JsonResponse:
    def __init__(self, resonse=""):
        self.resonse = resonse


if "local_controler_pb2" not in sys.modules:
    local_pb2 = types.ModuleType("local_controler_pb2")
    local_pb2.JsonResponse = _JsonResponse
    sys.modules["local_controler_pb2"] = local_pb2
if "local_controler_pb2_grpc" not in sys.modules:
    local_pb2_grpc = types.ModuleType("local_controler_pb2_grpc")
    local_pb2_grpc.LocalControllerServicer = object
    local_pb2_grpc.LocalControllerStub = object
    sys.modules["local_controler_pb2_grpc"] = local_pb2_grpc

from canyonos_core.controller.local_controller import LocalController


class _FakeRedis:
    def __init__(self):
        self.strings = {}

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)


def _build_controller(redis, publish_ready=False):
    servicer = SimpleNamespace(request_queue=[])
    with (
        patch(
            "canyonos_core.controller.local_controller.start_server",
            return_value=(MagicMock(), servicer),
        ),
        patch(
            "canyonos_core.controller.local_controller.RedisClient",
            return_value=redis,
        ),
        patch("canyonos_core.controller.local_controller.threading.Thread"),
        patch.object(LocalController, "_start_llm_proxy", return_value=None),
        patch.object(LocalController, "_load_agent", return_value=None),
    ):
        return LocalController(port=50051, publish_ready=publish_ready)


class LocalControllerReadinessTests(unittest.TestCase):
    def test_publish_ready_false_does_not_write_status(self):
        redis = _FakeRedis()
        _build_controller(redis, publish_ready=False)
        self.assertEqual(redis.strings, {})

    def test_mark_ready_writes_healthy_to_controller_status_key(self):
        redis = _FakeRedis()
        controller = _build_controller(redis, publish_ready=False)
        controller.mark_ready()
        self.assertEqual(
            redis.strings, {"controller:localhost:50051:status": "healthy"}
        )

    def test_mark_failed_writes_failed_to_controller_status_key(self):
        redis = _FakeRedis()
        controller = _build_controller(redis, publish_ready=False)
        controller.mark_failed()
        self.assertEqual(
            redis.strings, {"controller:localhost:50051:status": "failed"}
        )


if __name__ == "__main__":
    unittest.main()
