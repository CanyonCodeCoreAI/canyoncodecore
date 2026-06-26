import unittest

from tests.support.runtime_fakes import FakeController
from tests.support.runtime_fakes import make_instance
from ventis.controller.runtime_manager import RuntimeManager


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = RuntimeManager(self.controller, self.controller.redis)

    def write_instance(self, instance):
        key = self.manager._instance_key(
            instance["provider"],
            instance["agent_name"],
            int(instance["replica_index"]),
        )
        self.controller.redis.hset_multiple(key, instance)
        self.controller.redis.sadd(
            f"agent:{instance['agent_name']}:instances",
            self.manager._instance_id_from_record(instance),
        )

    def docker_run_calls(self):
        return [call for call in self.controller.run_calls if call[0][:2] == ["docker", "run"]]

    def test_existing_runtime_is_reused_without_new_docker_run(self):
        instance = make_instance("Alpha", 0, host_port=8000)
        self.controller.runtime_ids.add(instance["runtime_id"])
        self.write_instance(instance)

        self.manager.ensure_instances([{"name": "Alpha", "provider": "local", "replicas": 1}])

        self.assertEqual(self.docker_run_calls(), [])
        self.assertEqual(
            self.controller.redis.hgetall("agent_instance:local:Alpha:0")["endpoint"],
            "localhost:8000",
        )

    def test_missing_runtime_is_recreated_on_previous_port(self):
        self.write_instance(make_instance("Beta", 0, host_port=9100))

        self.manager.ensure_instances([{"name": "Beta", "provider": "local", "replicas": 1}])

        docker_run = self.docker_run_calls()[0][0]
        self.assertIn("9100:50051", docker_run)
        self.assertEqual(
            self.controller.redis.hgetall("agent_instance:local:Beta:0")["endpoint"],
            "localhost:9100",
        )

    def test_new_instances_get_incrementing_ports(self):
        self.manager.ensure_instances([{"name": "Beta", "provider": "local", "replicas": 3}])

        endpoints = [
            self.controller.redis.hgetall(f"agent_instance:local:Beta:{index}")["endpoint"]
            for index in range(3)
        ]

        self.assertEqual(endpoints, ["localhost:8000", "localhost:8001", "localhost:8002"])

    def test_stale_runtime_is_removed_before_recreate(self):
        stale = make_instance("Beta", 0, host_port=8000)
        stale["runtime_id"] = "stale-runtime"
        self.write_instance(stale)

        self.manager.ensure_instances([{"name": "Beta", "provider": "local", "replicas": 1}])

        remove_calls = [call for call in self.controller.run_calls if call[0][:3] == ["docker", "rm", "-f"]]
        self.assertEqual(remove_calls[0][0], ["docker", "rm", "-f", "stale-runtime"])

    def test_recreate_updates_controller_tracking_once(self):
        self.manager.ensure_instances([{"name": "Beta", "provider": "local", "replicas": 1}])
        self.manager.ensure_instances([{"name": "Beta", "provider": "local", "replicas": 1}])

        self.assertEqual(self.controller.containers["Beta"], ["ventis-local-beta-0"])

    def test_remove_instance_deletes_record_membership_and_runtime_tracking(self):
        instance = make_instance("Gamma", 0, host_port=8000)
        self.controller.runtime_ids.add(instance["runtime_id"])
        self.controller.containers = {"Gamma": [instance["runtime_id"]]}
        self.write_instance(instance)

        self.manager.remove_instance("local:Gamma:0")

        self.assertEqual(self.controller.redis.hgetall("agent_instance:local:Gamma:0"), {})
        self.assertEqual(self.controller.redis.smembers("agent:Gamma:instances"), set())
        self.assertEqual(self.controller.containers["Gamma"], [])
        self.assertFalse(self.controller.runtime_ids)

    def test_remove_missing_instance_is_noop(self):
        self.manager.remove_instance("local:Missing:0")

        self.assertEqual(self.controller.run_calls, [])

    def test_runtime_exists_returns_false_without_runtime_id(self):
        self.assertFalse(self.manager._runtime_exists({"host": "localhost"}))

    def test_launch_container_sets_runtime_environment_and_resources(self):
        spec = {
            "name": "Worker",
            "provider": "local",
            "replicas": 1,
            "redis_port": 6380,
            "resources": {"cpu": 2, "memory": 1024, "gpu": 1},
        }

        self.manager.ensure_instances([spec])

        docker_run = self.docker_run_calls()[0][0]
        self.assertIn("8000:50051", docker_run)
        self.assertIn("VENTIS_AGENT_HOST=host.docker.internal", docker_run)
        self.assertIn("VENTIS_AGENT_PORT=8000", docker_run)
        self.assertIn("VENTIS_REDIS_HOST=host.docker.internal", docker_run)
        self.assertIn("VENTIS_REDIS_PORT=6380", docker_run)
        self.assertIn("--cpus", docker_run)
        self.assertIn("2", docker_run)
        self.assertIn("--memory", docker_run)
        self.assertIn("1024m", docker_run)
        self.assertIn("--gpus", docker_run)
        self.assertIn("1", docker_run)

    def test_launch_container_raises_when_docker_run_fails(self):
        def failing_run(cmd, host, user=None):
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

        self.controller._run_cmd = failing_run

        with self.assertRaisesRegex(RuntimeError, "Failed to launch"):
            self.manager.ensure_instances([{"name": "Broken", "provider": "local", "replicas": 1}])

    def test_workflow_container_exposes_runtime_managed_api_port(self):
        self.manager.ensure_instances(
            [{"name": "Workflow", "provider": "local", "type": "workflow", "replicas": 1}]
        )

        docker_run = self.docker_run_calls()[0][0]

        self.assertIn("8000:50051", docker_run)
        self.assertIn("8080:8080", docker_run)


if __name__ == "__main__":
    unittest.main()
