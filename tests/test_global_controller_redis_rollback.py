import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller.global_controller import GlobalController


class RedisContainerRollbackTests(unittest.TestCase):
    def test_constructor_failure_removes_an_already_launched_redis_container(self):
        config = {
            "project_id": "test-project",
            "agents": [
                {
                    "name": "First",
                    "host": "10.0.0.1",
                    "user": "runner",
                    "replicas": 1,
                }
            ],
        }
        calls = []
        failure = RuntimeError("spec write failed")

        def fake_run_cmd(_controller, cmd, host, user=None):
            calls.append((host, cmd[:]))
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(GlobalController, "_load_config", return_value=config), patch(
            "canyonos_core.controller.global_controller.resolve_env_file",
            return_value=None,
        ), patch(
            "canyonos_core.controller.global_controller.RedisClient",
            return_value=MagicMock(),
        ), patch(
            "canyonos_core.controller.global_controller._wait_for_redis"
        ), patch(
            "canyonos_core.controller.global_controller.assign_project_id"
        ), patch.object(
            GlobalController, "_database_url", return_value=None
        ), patch.object(
            GlobalController, "_cleanup_stale_containers"
        ), patch.object(
            GlobalController, "_run_cmd", new=fake_run_cmd
        ), patch(
            "canyonos_core.controller.global_controller.write_agent_specs",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(RuntimeError, "spec write failed") as raised:
                GlobalController("config.yaml")

        self.assertIs(raised.exception, failure)
        self.assertIn(
            ("10.0.0.1", ["docker", "stop", "canyonos-redis-10-0-0-1"]),
            calls,
        )
        self.assertIn(
            ("10.0.0.1", ["docker", "rm", "canyonos-redis-10-0-0-1"]),
            calls,
        )

    def test_later_launch_failure_rolls_back_an_earlier_owned_container(self):
        controller = GlobalController.__new__(GlobalController)
        controller.controllers = [
            {"name": "First", "host": "10.0.0.1", "user": "runner", "replicas": 1},
            {"name": "Second", "host": "10.0.0.2", "user": "runner", "replicas": 1},
        ]
        controller.redis_containers = {}
        controller.node_redis = {}
        controller.redis = None
        calls = []

        def fake_run_cmd(cmd, host, user=None):
            calls.append((host, cmd[:]))
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            if cmd[:2] == ["docker", "run"] and host == "10.0.0.2":
                return subprocess.CompletedProcess(cmd, 1, "", "temporary launch failure")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        controller._run_cmd = fake_run_cmd

        with patch(
            "canyonos_core.controller.global_controller.RedisClient",
            return_value=MagicMock(),
        ), patch("canyonos_core.controller.global_controller._wait_for_redis"):
            with self.assertRaisesRegex(
                RuntimeError, "Failed to launch Redis on 10.0.0.2 after 3 attempts"
            ):
                controller._launch_redis_containers()

        second_host_runs = [
            cmd for host, cmd in calls if host == "10.0.0.2" and cmd[:2] == ["docker", "run"]
        ]
        self.assertEqual(len(second_host_runs), 3)
        self.assertIn(
            ("10.0.0.1", ["docker", "stop", "canyonos-redis-10-0-0-1"]),
            calls,
        )
        self.assertIn(
            ("10.0.0.1", ["docker", "rm", "canyonos-redis-10-0-0-1"]),
            calls,
        )
        self.assertEqual(controller.redis_containers, {})


if __name__ == "__main__":
    unittest.main()
