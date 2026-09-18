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
        self.hashes = {}

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def hset_multiple(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)


def _build_controller(redis, publish_ready=False, agent=None):
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
        patch.object(LocalController, "_load_agent", return_value=agent),
    ):
        return LocalController(port=50051, publish_ready=publish_ready)


def _beat_once(controller):
    """Run exactly one pass of the metrics loop, then let it stop."""
    controller._metrics_stop_event.wait = lambda *args, **kwargs: (
        controller._metrics_stop_event.set()
    )
    controller._metrics_loop()


STATUS_KEY = "controller:localhost:50051:status"


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

    def test_a_declared_agent_that_fails_to_load_publishes_failed(self):
        # The status used to be written before the load was attempted, so this
        # container reported healthy, GlobalController called the controller
        # ready, `deploy` printed "N agent(s) ready", and the port was only
        # found broken at `test`.
        redis = _FakeRedis()
        with patch.dict(
            os.environ,
            {
                "CANYONOS_AGENT_NAME": "RagChatbotAgent",
                "CANYONOS_AGENT_FILE": "chatbot.py",
            },
        ):
            _build_controller(redis, publish_ready=True, agent=None)
        self.assertEqual(redis.strings["controller:localhost:50051:status"], "failed")

    def test_a_declared_agent_that_loads_publishes_healthy(self):
        redis = _FakeRedis()
        with patch.dict(
            os.environ,
            {
                "CANYONOS_AGENT_NAME": "RagChatbotAgent",
                "CANYONOS_AGENT_FILE": "chatbot.py",
            },
        ):
            _build_controller(redis, publish_ready=True, agent=object())
        self.assertEqual(redis.strings["controller:localhost:50051:status"], "healthy")

    def test_an_agentless_container_publishes_healthy(self):
        # A workflow container declares no agent; None from _load_agent is not a
        # failure there.
        redis = _FakeRedis()
        with patch.dict(
            os.environ, {"CANYONOS_AGENT_NAME": "", "CANYONOS_AGENT_FILE": ""}
        ):
            _build_controller(redis, publish_ready=True, agent=None)
        self.assertEqual(redis.strings["controller:localhost:50051:status"], "healthy")

    def test_a_heartbeat_does_not_resurrect_a_failed_status(self):
        # The heartbeat published a literal "healthy", so it undid mark_failed()
        # within one interval: the container that failed to load its agent was
        # announced ready again a few seconds later, and `deploy` believed it.
        redis = _FakeRedis()
        with patch.dict(
            os.environ,
            {
                "CANYONOS_AGENT_NAME": "RagChatbotAgent",
                "CANYONOS_AGENT_FILE": "chatbot.py",
            },
        ):
            controller = _build_controller(redis, publish_ready=True, agent=None)
        self.assertEqual(redis.strings[STATUS_KEY], "failed")
        _beat_once(controller)
        self.assertEqual(redis.strings[STATUS_KEY], "failed")

    def test_a_heartbeat_before_readiness_does_not_announce_healthy(self):
        # The heartbeat starts before the agent is loaded. Until something marks
        # the container one way or the other it is starting, not ready.
        redis = _FakeRedis()
        controller = _build_controller(redis, publish_ready=False)
        _beat_once(controller)
        self.assertEqual(redis.strings[STATUS_KEY], "starting")

    def test_published_metrics_carry_the_real_status(self):
        redis = _FakeRedis()
        controller = _build_controller(redis, publish_ready=False)
        controller.mark_failed()
        _beat_once(controller)
        self.assertEqual(redis.hashes[controller._metrics_key]["status"], "failed")

    def test_mark_failed_writes_failed_to_controller_status_key(self):
        redis = _FakeRedis()
        controller = _build_controller(redis, publish_ready=False)
        controller.mark_failed()
        self.assertEqual(redis.strings, {"controller:localhost:50051:status": "failed"})


if __name__ == "__main__":
    unittest.main()
