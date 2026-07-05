import queue
import unittest
import json

from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import install_grpc_stubs

install_grpc_stubs()

from ventis.controller.local_controller import LocalController
from ventis.controller.local_controller_frontend import build_queue_item


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

    def test_refresh_runtime_policy_rebuilds_pending_queue_with_new_agent_priority(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.agent_host = "host.docker.internal"
        controller.public_port = "8000"
        controller.redis = FakeRedis()
        controller.agent_priority = 9
        controller._active_requests = 1
        controller.request_queue = queue.PriorityQueue()
        controller._publish_queue_depth = lambda: None
        controller.servicer = type(
            "Servicer",
            (),
            {"set_agent_priority": lambda self, priority: setattr(self, "priority", priority)},
        )()
        controller.redis.hset("agent:ExampleAgent:", "priority", "2")

        first = json.dumps({"future_id": "first", "service": "A", "function": "x", "session_priority": 1, "agent_priority": 9})
        second = json.dumps({"future_id": "second", "service": "A", "function": "x", "session_priority": 1, "agent_priority": 9})
        third = json.dumps({"future_id": "third", "service": "A", "function": "x", "session_priority": 0, "agent_priority": 9})
        controller.request_queue.put(build_queue_item(first, 1, 9, enqueue_order=10))
        controller.request_queue.put(build_queue_item(second, 1, 9, enqueue_order=11))
        controller.request_queue.put(build_queue_item(third, 0, 9, enqueue_order=12))

        changed = LocalController._refresh_runtime_policy(controller)

        self.assertTrue(changed)
        self.assertEqual(controller.agent_priority, 2)
        self.assertEqual(controller.servicer.priority, 2)
        ordered = [json.loads(controller.request_queue.get()[3])["future_id"] for _ in range(3)]
        self.assertEqual(ordered, ["third", "first", "second"])
        self.assertEqual(controller._active_requests, 1)

    def test_refresh_runtime_policy_noops_when_priority_unchanged(self):
        controller = LocalController.__new__(LocalController)
        controller.agent_name = "ExampleAgent"
        controller.redis = FakeRedis()
        controller.agent_priority = 4
        controller.request_queue = queue.PriorityQueue()
        controller._publish_queue_depth = lambda: None
        controller.servicer = type(
            "Servicer",
            (),
            {"set_agent_priority": lambda self, priority: setattr(self, "priority", priority)},
        )()
        controller.redis.hset("agent:ExampleAgent:", "priority", "4")
        controller.request_queue.put(build_queue_item('{"future_id":"only"}', 1, 4, enqueue_order=5))

        changed = LocalController._refresh_runtime_policy(controller)

        self.assertFalse(changed)
        self.assertEqual(controller.request_queue.get()[2], 5)


if __name__ == "__main__":
    unittest.main()
