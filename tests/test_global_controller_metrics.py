import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "ventis", "templates", "grpc_stubs"
        )
    ),
)

from ventis.controller.global_controller import GlobalController
from ventis.controller.instance_manager import InstanceManager


class MergeInstanceMetricsTests(unittest.TestCase):
    def _controller(self):
        return SimpleNamespace(instance_manager=InstanceManager(None))

    def test_agent_id_passes_through_from_instance_record(self):
        controller = self._controller()
        instance = {
            "agent_id": "1f2e3d4c5b6a7988fedcba9876543210",
            "agent_name": "AgentA",
            "provider": "local",
            "replica_index": "0",
            "host": "localhost",
            "host_port": "50051",
        }
        metrics = {"status": "healthy", "cpu_percent": "1.0"}

        row = GlobalController._merge_instance_metrics(controller, instance, metrics)

        self.assertEqual(row["agent_id"], "1f2e3d4c5b6a7988fedcba9876543210")
        self.assertIsNone(row["queue_length"])
        self.assertIsNone(row["requests_served"])
        self.assertIsNone(row["throughput"])
        self.assertIsNone(row["disk_percent"])
        self.assertIsNone(row["memory_percent"])

    def test_llm_errors_read_from_node_redis_and_reset_default(self):
        controller = self._controller()
        instance = {
            "agent_id": "1f2e3d4c5b6a7988fedcba9876543210",
            "agent_name": "AgentA",
            "provider": "local",
            "replica_index": "0",
            "host": "localhost",
            "host_port": "50051",
        }
        metrics = {"status": "healthy", "cpu_percent": "1.0"}

        class _FakeNodeRedis:
            def get(self, key):
                if key == "1f2e3d4c5b6a7988fedcba9876543210:llm_errors":
                    return "3"
                return None

        row = GlobalController._merge_instance_metrics(
            controller, instance, metrics, _FakeNodeRedis()
        )
        self.assertEqual(row["errors"], 3)

    def test_llm_errors_default_to_zero_without_node_redis(self):
        controller = self._controller()
        instance = {
            "agent_id": "1f2e3d4c5b6a7988fedcba9876543210",
            "agent_name": "AgentA",
            "provider": "local",
            "replica_index": "0",
            "host": "localhost",
            "host_port": "50051",
        }
        metrics = {"status": "healthy", "cpu_percent": "1.0"}

        row = GlobalController._merge_instance_metrics(controller, instance, metrics)
        self.assertEqual(row["errors"], 0)

if __name__ == "__main__":
    unittest.main()
