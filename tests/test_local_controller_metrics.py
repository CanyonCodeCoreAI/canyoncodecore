import os
import sys
import threading
import unittest
import json
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "canyonos_core", "templates", "grpc_stubs"
        )
    ),
)

from canyonos_core.controller.local_controller import LocalController


def _bind_failure_marker(controller):
    controller._mark_future_failed = lambda future_id, error, origin=None: (
        LocalController._mark_future_failed(controller, future_id, error, origin)
    )
    controller._send_result_callback = (
        lambda origin, future_id, result="", failed=0, error_message="": LocalController._send_result_callback(
            controller, origin, future_id, result, failed, error_message
        )
    )
    controller._fan_out_to_consumers = (
        lambda future_id, result=None, failed=0, error_message="": LocalController._fan_out_to_consumers(
            controller, future_id, result, failed, error_message
        )
    )
    return controller


class _FakeRedisClient:
    def __init__(self):
        self.counters = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


class _FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.client = _FakeRedisClient()

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hincrby(self, name, field, amount=1):
        bucket = self.hashes.setdefault(name, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return bucket[field]

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def smembers(self, key):
        return set()


class LocalControllerMetricsTests(unittest.TestCase):
    def test_collect_metrics_returns_expected_keys(self):
        controller = SimpleNamespace(
            _executor=ThreadPoolExecutor(max_workers=1),
            _metrics_interval=5,
        )
        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            metrics = LocalController._collect_metrics(controller)
        self.assertEqual(metrics["status"], "healthy")
        self.assertEqual(
            set(metrics.keys()),
            {
                "status",
                "cpu_percent",
                "gpu_percent",
                "disk_percent",
                "memory_percent",
                "uptime_seconds",
                "queue_length",
                "updated_at",
            },
        )
        float(metrics["cpu_percent"])
        float(metrics["gpu_percent"])
        float(metrics["disk_percent"])
        float(metrics["memory_percent"])
        float(metrics["uptime_seconds"])
        int(metrics["queue_length"])
        float(metrics["updated_at"])

    def test_metrics_loop_writes_hash_and_refreshes_status(self):
        redis = _FakeRedis()
        stop_event = threading.Event()
        controller = SimpleNamespace(
            redis=redis,
            _metrics_key="controller:localhost:50051:metrics",
            _status_key="controller:localhost:50051:status",
            _metrics_stop_event=stop_event,
            _metrics_interval=5,
            _collect_metrics=lambda: {
                "status": "healthy",
                "cpu_percent": "1.0",
                "gpu_percent": "0.0",
                "uptime_seconds": "10.0",
                "updated_at": "100.0",
            },
        )

        def stop_after_one_tick(timeout):
            stop_event.set()

        stop_event.wait = stop_after_one_tick

        LocalController._metrics_loop(controller)

        self.assertEqual(
            redis.hgetall("controller:localhost:50051:metrics")["cpu_percent"], "1.0"
        )
        self.assertEqual(redis.get("controller:localhost:50051:status"), "healthy")

    def test_execute_locally_writes_gpu_resource_to_future_hash(self):
        redis = _FakeRedis()
        agent = SimpleNamespace(greet=lambda name: f"hello {name}")
        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
            agent=agent,
            agent_name="Greeter",
            agent_id="1f2e3d4c5b6a7988fedcba9876543210",
            _my_endpoint="localhost:50051",
            _metrics_key="controller:localhost:50051:metrics",
            _resolve_future_args=lambda args: args,
        ))

        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=17.5
        ):
            LocalController._execute_locally(
                controller, "Greeter", "greet", {"name": "world"}, "future-1"
            )

        self.assertEqual(redis.hget("future:future-1", "gpu_resource"), 17.5)
        self.assertEqual(redis.hget("future:future-1", "result"), "hello world")
        self.assertEqual(
            redis.hget("future:future-1", "agent"),
            "1f2e3d4c5b6a7988fedcba9876543210",
        )
        self.assertNotIn("future:future-1:metrics", redis.hashes)
        self.assertEqual(
            redis.hget("controller:localhost:50051:metrics", "requests_served"), 1
        )

    def test_execute_locally_counts_failed_executions_as_served(self):
        redis = _FakeRedis()

        def boom(name):
            raise ValueError("nope")

        agent = SimpleNamespace(greet=boom)
        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
            agent=agent,
            agent_name="Greeter",
            agent_id="aabbccddeeff00112233445566778899",
            _my_endpoint="localhost:50051",
            _metrics_key="controller:localhost:50051:metrics",
            _resolve_future_args=lambda args: args,
        ))

        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            LocalController._execute_locally(
                controller, "Greeter", "greet", {"name": "world"}, "future-2"
            )

        self.assertEqual(redis.hget("future:future-2", "error"), "nope")
        self.assertEqual(redis.hget("future:future-2", "result"), "")
        self.assertEqual(
            redis.hget("future:future-2", "failed"), 1
        )
        self.assertNotIn("future:future-2:metrics", redis.hashes)
        self.assertEqual(
            redis.hget("controller:localhost:50051:metrics", "requests_served"), 1
        )
        self.assertEqual(
            redis.hget("controller:localhost:50051:metrics", "full_failures"), 1
        )

    def test_execute_locally_marks_missing_agent_as_failed(self):
        redis = _FakeRedis()
        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
            agent=None,
            agent_name="MissingAgent",
            agent_id="agent-1",
            _my_endpoint="localhost:50051",
            _metrics_key="controller:localhost:50051:metrics",
        ))

        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            LocalController._execute_locally(
                controller, "MissingAgent", "greet", {}, "future-3"
            )

        self.assertEqual(
            redis.hget("future:future-3", "error"), "No agent loaded"
        )
        self.assertEqual(redis.hget("future:future-3", "failed"), 1)
        self.assertNotIn("future:future-3:metrics", redis.hashes)

    def test_remote_execution_failure_sends_error_callback(self):
        redis = _FakeRedis()
        stub = SimpleNamespace(WriteResult=MagicMock())

        def boom():
            raise ValueError("remote nope")

        controller = _bind_failure_marker(SimpleNamespace(
            redis=redis,
            agent=SimpleNamespace(greet=boom),
            agent_name="Greeter",
            agent_id="agent-1",
            _my_endpoint="target:50051",
            _metrics_key="controller:target:50051:metrics",
            _resolve_future_args=lambda args: args,
            _get_remote_stub=lambda endpoint: stub,
        ))

        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            LocalController._execute_locally(
                controller,
                "Greeter",
                "greet",
                {},
                "future-4",
                origin="origin:50051",
            )

        self.assertEqual(redis.hget("future:future-4", "error"), "remote nope")
        payload = json.loads(stub.WriteResult.call_args.args[0].resonse)
        self.assertEqual(payload["future_id"], "future-4")
        self.assertEqual(payload["result"], "")
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["error"], "remote nope")
        # The callback fires only after the finally block writes final metrics,
        # so the snapshot sent to origin carries the full execution record.
        self.assertEqual(payload["agent"], "agent-1")
        self.assertIn("finished_at", payload)
        self.assertIn("cpu_resource", payload)
        self.assertEqual(payload["gpu_resource"], 0.0)

    def test_callback_fires_once_and_only_after_final_metrics_written(self):
        redis = _FakeRedis()
        seen_at_callback_time = {}

        def spy_send_result_callback(origin, future_id, result=None, failed=0, error_message=""):
            seen_at_callback_time["snapshot"] = dict(redis.hashes.get(f"future:{future_id}", {}))
            seen_at_callback_time["calls"] = seen_at_callback_time.get("calls", 0) + 1

        controller = SimpleNamespace(
            redis=redis,
            agent=SimpleNamespace(greet=lambda name: f"hello {name}"),
            agent_name="Greeter",
            agent_id="agent-1",
            _my_endpoint="target:50051",
            _metrics_key="controller:target:50051:metrics",
            _resolve_future_args=lambda args: args,
            _mark_future_failed=lambda future_id, error, origin=None: None,
            _send_result_callback=spy_send_result_callback,
        )

        with patch(
            "canyonos_core.controller.local_controller.read_gpu_percent", return_value=0.0
        ):
            LocalController._execute_locally(
                controller,
                "Greeter",
                "greet",
                {"name": "world"},
                "future-5",
                origin="origin:50051",
            )

        self.assertEqual(seen_at_callback_time["calls"], 1)
        self.assertIn("finished_at", seen_at_callback_time["snapshot"])
        self.assertIn("cpu_resource", seen_at_callback_time["snapshot"])
        self.assertIn("gpu_resource", seen_at_callback_time["snapshot"])
        self.assertIn("agent", seen_at_callback_time["snapshot"])


if __name__ == "__main__":
    unittest.main()
