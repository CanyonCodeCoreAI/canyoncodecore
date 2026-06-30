import queue
import unittest

from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import install_grpc_stubs

install_grpc_stubs()

from ventis.controller.local_controller import LocalController


class LocalControllerQueueDepthTests(unittest.TestCase):
    def test_publish_queue_depth_writes_current_queue_size(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller.redis = FakeRedis()
        controller._active_requests = 0
        controller.request_queue = queue.Queue()
        controller.request_queue.put("a")
        controller.request_queue.put("b")

        LocalController._publish_queue_depth(controller)

        self.assertEqual(
            controller.redis.get("queue_depth:ExampleAgent:host.docker.internal:8000"),
            "2",
        )

    def test_publish_queue_depth_includes_active_requests(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller.redis = FakeRedis()
        controller._active_requests = 3
        controller.request_queue = queue.Queue()
        controller.request_queue.put("a")

        LocalController._publish_queue_depth(controller)

        self.assertEqual(
            controller.redis.get("queue_depth:ExampleAgent:host.docker.internal:8000"),
            "4",
        )

    def test_publish_active_work_writes_current_active_requests(self):
        controller = LocalController.__new__(LocalController)
        controller.redis = FakeRedis()
        controller._active_requests = 3
        controller._active_work_key_name = "controller:host.docker.internal:8000:active_work"

        LocalController._publish_active_work(controller)

        self.assertEqual(
            controller.redis.get("controller:host.docker.internal:8000:active_work"),
            "3",
        )

    def test_update_lifecycle_signal_marks_delete_ready_only_after_drain(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller._my_endpoint = "host.docker.internal:8000"
        controller._shutting_down = False
        controller._active_requests = 0
        controller._lifecycle_signal_key = "controller:host.docker.internal:8000:lifecycle"
        controller.redis = FakeRedis()
        controller.request_queue = queue.Queue()
        controller.redis.hset(
            "routing_table:status",
            "ExampleAgent",
            '{"host.docker.internal:8000":"Shutting down"}',
        )

        LocalController._update_lifecycle_signal(controller)

        self.assertEqual(
            controller.redis.get("controller:host.docker.internal:8000:lifecycle"),
            "Delete Ready",
        )

    def test_update_lifecycle_signal_blocks_stateful_affine_inflight_request(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller._my_endpoint = "host.docker.internal:8000"
        controller._shutting_down = False
        controller._active_requests = 0
        controller._lifecycle_signal_key = "controller:host.docker.internal:8000:lifecycle"
        controller.redis = FakeRedis()
        controller.request_queue = queue.Queue()
        controller.redis.hset(
            "routing_table:status",
            "ExampleAgent",
            '{"host.docker.internal:8000":"Shutting down"}',
        )
        controller.redis.hset("routing_table:stateful", "ExampleAgent", "true")
        controller.redis.hset("affinity:req-1", "ExampleAgent", "host.docker.internal:8000")
        controller.redis.set("request:req-1:status", "running")

        LocalController._update_lifecycle_signal(controller)

        self.assertIsNone(
            controller.redis.get("controller:host.docker.internal:8000:lifecycle")
        )

    def test_stop_without_agent_name_does_not_write_bogus_queue_key(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = None
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller.redis = FakeRedis()
        controller._status_key = "controller:host.docker.internal:8000:status"
        controller._active_work_key_name = "controller:host.docker.internal:8000:active_work"
        controller._lifecycle_signal_key = "controller:host.docker.internal:8000:lifecycle"
        controller._executor = type("Executor", (), {"shutdown": lambda self, wait=True: None})()
        controller.server = type("Server", (), {"stop": lambda self, code: None})()

        LocalController.stop(controller)

        self.assertEqual(controller.redis.get(controller._status_key), "stopped")
        self.assertEqual(
            controller.redis.get("controller:host.docker.internal:8000:active_work"),
            "0",
        )
        self.assertEqual(controller.redis.scan_keys("queue_depth:*"), [])


if __name__ == "__main__":
    unittest.main()
