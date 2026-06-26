import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import install_grpc_stubs
from tests.support.runtime_fakes import make_global_controller
from tests.support.runtime_fakes import make_instance

install_grpc_stubs()

from ventis.controller.global_controller import GlobalController


class GlobalControllerRuntimeBackedTests(unittest.TestCase):
    def test_wait_for_healthy_reads_local_status_from_host_redis(self):
        instance = make_instance("Alpha", 0, host="localhost", host_port=8000)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8000:status",
            "healthy",
        )

        GlobalController._wait_for_healthy(controller, timeout=1, interval=0)

        self.assertEqual(controller._last_status, {("localhost", "8000"): "healthy"})

    def test_poll_controllers_calls_healthy_hook_for_runtime_instance(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8001:status",
            "healthy",
        )

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [("Beta", "localhost", "8001")])
        self.assertEqual(controller._unhealthy_calls, [])

    def test_poll_controllers_calls_unhealthy_hook_for_missing_status(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [])
        self.assertEqual(controller._unhealthy_calls, [("Beta", "localhost", "8001")])
        self.assertEqual(controller._last_status[("localhost", "8001")], "unknown")

    def test_poll_controllers_uses_remote_host_as_status_key(self):
        instance = make_instance("Remote", 0, host="10.0.0.7", host_port=9000)
        controller = make_global_controller([instance])
        controller.node_redis["10.0.0.7"] = FakeRedis()
        controller.node_redis["10.0.0.7"].set(
            "controller:10.0.0.7:9000:status",
            "healthy",
        )

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [("Remote", "10.0.0.7", "9000")])

    def test_trigger_cleanup_broadcasts_to_runtime_endpoints(self):
        instance = make_instance("Gamma", 0, host="localhost", host_port=8002)
        controller = make_global_controller([instance])
        controller.redis.sadd("request:completed", "req-1")
        messages = []

        class Stub:
            def Cleanup(self, message):
                messages.append(message)

        controller._get_lc_stub = lambda endpoint: Stub()

        GlobalController._trigger_cleanup(controller)

        self.assertEqual([json.loads(message.resonse) for message in messages], [{"request_id": "req-1"}])
        self.assertEqual(controller.redis.smembers("request:completed"), set())

    def test_trigger_cleanup_noops_without_completed_requests(self):
        instance = make_instance("Gamma", 0, host="localhost", host_port=8002)
        controller = make_global_controller([instance])
        calls = []
        controller._get_lc_stub = lambda endpoint: calls.append(endpoint)

        GlobalController._trigger_cleanup(controller)

        self.assertEqual(calls, [])

    def test_stop_docker_agents_delegates_to_runtime_manager(self):
        instance = make_instance("Delta", 0, host="localhost", host_port=8003)
        controller = make_global_controller([instance])
        controller.containers = {"Delta": [instance["runtime_id"]]}
        removed = []
        controller.runtime_manager.remove_instance = lambda instance_id: removed.append(instance_id)

        GlobalController._stop_docker_agents(controller)

        self.assertEqual(removed, ["local:Delta:0"])
        self.assertEqual(controller._run_cmd_calls, [])
        self.assertEqual(controller.containers, {})

    def test_agent_host_key_maps_localhost_for_container_status(self):
        controller = make_global_controller([])

        self.assertEqual(
            GlobalController._agent_host_key(controller, "localhost"),
            "host.docker.internal",
        )
        self.assertEqual(
            GlobalController._agent_host_key(controller, "10.0.0.4"),
            "10.0.0.4",
        )

    def test_launch_redis_containers_uses_runtime_nodes(self):
        controller = make_global_controller([])
        controller.runtime_manager.list_runtime_nodes = lambda agent_specs=None: {
            "localhost": {"user": None, "redis_port": 6379},
            "10.0.0.7": {"user": "ubuntu", "redis_port": 6380},
        }
        created_clients = []

        with patch(
            "ventis.controller.global_controller.RedisClient",
            side_effect=lambda **kwargs: created_clients.append(kwargs) or FakeRedis(),
        ):
            GlobalController._launch_redis_containers(controller)

        self.assertEqual(
            controller.redis_containers,
            {
                "localhost": "ventis-redis-localhost",
                "10.0.0.7": "ventis-redis-10-0-0-7",
            },
        )
        self.assertEqual(created_clients, [{"host": "localhost", "port": 6379}, {"host": "10.0.0.7", "port": 6380}])

    def test_ensure_host_redis_reuses_existing_client(self):
        controller = make_global_controller([])
        existing = FakeRedis()
        controller.node_redis["localhost"] = existing

        result = GlobalController.ensure_host_redis(controller, "localhost")

        self.assertIs(result, existing)
        self.assertEqual(controller._run_cmd_calls, [])

    def test_ensure_host_redis_exits_when_docker_run_fails(self):
        controller = make_global_controller([])
        controller._run_cmd = lambda cmd, host, user=None: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="docker failed",
        )

        with self.assertRaises(SystemExit):
            GlobalController.ensure_host_redis(controller, "localhost")

    def test_ssh_base_cmd_uses_configured_private_key(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}

        self.assertEqual(
            GlobalController._ssh_base_cmd(controller),
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-i",
                "/tmp/test.pem",
            ],
        )

    def test_run_cmd_uses_ssh_options_for_remote_host(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}

        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run_mock:
            GlobalController._run_cmd(controller, ["docker", "ps"], "10.0.0.7", "ec2-user")

        run_mock.assert_called_once_with(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-i",
                "/tmp/test.pem",
                "ec2-user@10.0.0.7",
                "sudo docker ps",
            ],
            capture_output=True,
            text=True,
        )

    def test_ensure_host_redis_prepares_remote_docker_first(self):
        controller = make_global_controller([])
        calls = []
        controller._ensure_remote_docker = lambda host, user=None: calls.append(
            ("prep", host, user)
        ) or SimpleNamespace(returncode=0, stdout="", stderr="")
        controller._run_cmd = lambda cmd, host, user=None: calls.append(
            ("run", cmd, host, user)
        ) or SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "ventis.controller.global_controller.RedisClient",
            side_effect=lambda **kwargs: FakeRedis(),
        ):
            GlobalController.ensure_host_redis(controller, "10.0.0.7", "ec2-user", 6380)

        self.assertEqual(calls[0], ("prep", "10.0.0.7", "ec2-user"))
        self.assertEqual(calls[1][0], "run")

    def test_wait_for_remote_ssh_retries_until_success(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}
        results = [
            SimpleNamespace(returncode=255, stdout="", stderr="Connection timed out"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]

        with patch("subprocess.run", side_effect=results) as run_mock:
            with patch("time.sleep", return_value=None):
                result = GlobalController._wait_for_remote_ssh(
                    controller, "10.0.0.7", "ec2-user", timeout=5, interval=0
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
