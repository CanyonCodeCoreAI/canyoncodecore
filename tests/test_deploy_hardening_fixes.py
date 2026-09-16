"""Regressions for four independent defects found reviewing the CAN-316 diff."""

import os
import socket
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller.cloud_provider_logic.Local import _runtime as local_runtime
from canyonos_core.controller.global_controller import GlobalController
from canyonos_core.controller.instance_manager import InstanceManager
from canyonos_core.controller.local_controller import LocalController


class AgentStartupOrderTests(unittest.TestCase):
    def test_the_agent_is_constructed_after_the_llm_proxy_is_listening(self):
        """An agent constructor may build an LLM client against the in-container
        proxy, so the proxy has to exist first."""
        order = []

        with patch.dict(
            os.environ, {"CANYONOS_AGENT_NAME": "Analyst", "CANYONOS_AGENT_PORT": "50051"}
        ), patch(
            "canyonos_core.controller.local_controller.start_server",
            return_value=(MagicMock(), MagicMock()),
        ), patch(
            "canyonos_core.controller.local_controller.RedisClient", return_value=MagicMock()
        ), patch.object(
            LocalController,
            "_start_llm_proxy",
            lambda _self, *_a: order.append("proxy") or MagicMock(),
        ), patch.object(
            LocalController,
            "_load_agent",
            lambda _self: order.append("agent") or MagicMock(),
        ):
            LocalController()

        self.assertEqual(order, ["proxy", "agent"])


class WorkflowPortPreflightTests(unittest.TestCase):
    def test_a_local_port_is_probed_on_the_host_not_our_own_loopback(self):
        """The controller runs in a container on a bridge network, so its own
        localhost is not where workflow ports are published."""
        probed = []

        def fake_connection(address, timeout=None):
            probed.append(address)
            raise OSError("refused")

        with patch.object(socket, "create_connection", fake_connection):
            self.assertFalse(local_runtime._port_check("localhost", 8080))
            self.assertFalse(local_runtime._port_check("127.0.0.1", 8080))

        self.assertEqual(probed, [(local_runtime.HOST_GATEWAY, 8080)] * 2)

    def test_a_remote_host_is_still_probed_directly(self):
        probed = []

        def fake_connection(address, timeout=None):
            probed.append(address)
            raise OSError("refused")

        with patch.object(socket, "create_connection", fake_connection):
            self.assertFalse(local_runtime._port_check("10.0.0.5", 8080))

        self.assertEqual(probed, [("10.0.0.5", 8080)])


class RedisLaunchRetryTests(unittest.TestCase):
    def test_a_failed_launch_clears_the_container_name_before_retrying(self):
        """docker leaves a Created container holding the name when the port bind
        fails, which would make every retry fail for the wrong reason."""
        config = {
            "project_id": "test-project",
            "agents": [{"name": "First", "host": "localhost", "replicas": 1}],
        }
        commands = []

        def fake_run_cmd(_controller, cmd, host, user=None):
            commands.append(cmd[:])
            if cmd[:2] == ["docker", "run"]:
                return subprocess.CompletedProcess(cmd, 125, "", "port is already allocated")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(GlobalController, "_load_config", return_value=config), patch(
            "canyonos_core.controller.global_controller.resolve_env_file", return_value=None
        ), patch(
            "canyonos_core.controller.global_controller.RedisClient", return_value=MagicMock()
        ), patch(
            "canyonos_core.controller.global_controller._wait_for_redis"
        ), patch(
            "canyonos_core.controller.global_controller.assign_project_id"
        ), patch.object(
            GlobalController, "_database_url", return_value=None
        ), patch.object(
            GlobalController, "_redis_container_healthy", return_value=False
        ), patch.object(
            GlobalController, "_run_cmd", fake_run_cmd
        ):
            with self.assertRaises(RuntimeError) as raised:
                GlobalController("unused.yaml")

        runs = [cmd for cmd in commands if cmd[:2] == ["docker", "run"]]
        removes = [cmd for cmd in commands if cmd[:3] == ["docker", "rm", "-f"]]
        self.assertEqual(len(runs), 3)
        self.assertGreaterEqual(len(removes), 3)
        # The real cause survives instead of "name is already in use".
        self.assertIn("port is already allocated", str(raised.exception))


class RuntimeReuseScanTests(unittest.TestCase):
    def _manager(self, run_cmd):
        controller = SimpleNamespace(
            containers={},
            config={},
            redis=MagicMock(),
            node_redis={},
            _run_cmd=run_cmd,
        )
        return InstanceManager(controller, redis_client=MagicMock())

    def test_an_unreachable_host_is_not_reusable_rather_than_fatal(self):
        def run_cmd(_cmd, host, user=None):
            raise RuntimeError(f"Command timed out after 180s on {host}: docker inspect")

        manager = self._manager(run_cmd)
        instance = {"runtime_id": "canyonos-agent-0", "host": "10.0.0.5", "provider": "EC2"}

        with self.assertLogs("canyonos_core.controller.instance_manager", level="WARNING"):
            self.assertFalse(manager._runtime_is_running(instance))

    def test_a_running_runtime_is_still_reported_as_reusable(self):
        def run_cmd(_cmd, _host, user=None):
            return subprocess.CompletedProcess([], 0, "true\n", "")

        manager = self._manager(run_cmd)
        instance = {"runtime_id": "canyonos-agent-0", "host": "localhost"}

        self.assertTrue(manager._runtime_is_running(instance))


if __name__ == "__main__":
    unittest.main()
