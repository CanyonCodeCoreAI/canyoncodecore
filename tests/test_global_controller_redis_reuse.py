"""Fix C: a restart must not unconditionally wipe and recreate each node's Redis container.

_launch_redis_containers() used to `docker run` a fresh canyonos-redis-<host> container on every
__init__, unconditionally -- wiping every `agent_instance:*` record InstanceManager needs to
recognize already-running EC2 replicas as reusable. ensure_instances()'s dedup logic was already
correct; it was just fed an empty Redis on every restart, so it reprovisioned everything from
scratch and orphaned the previous replicas. The fix: check whether the existing container is
already healthy before recreating it.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller.global_controller import GlobalController


def _bare_controller(controllers):
    controller = GlobalController.__new__(GlobalController)
    controller.controllers = controllers
    controller.redis_containers = {}
    controller.node_redis = {}
    controller.redis = None
    return controller


class RedisContainerReuseTests(unittest.TestCase):
    def _run(self, controller, inspect_stdout):
        run_calls = []

        def fake_run_cmd(cmd, host, user=None):
            run_calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=inspect_stdout, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_cmd = fake_run_cmd

        with patch("canyonos_core.controller.global_controller.RedisClient") as fake_redis_cls, patch(
            "canyonos_core.controller.global_controller._wait_for_redis"
        ):
            fake_redis_cls.return_value = MagicMock()
            controller._launch_redis_containers()

        return [c for c in run_calls if c[:2] == ["docker", "run"]]

    def test_a_healthy_existing_container_is_reused_not_recreated(self):
        controller = _bare_controller(
            [{"name": "Workflow", "replicas": 1, "redis_port": 6379}]
        )

        docker_run_calls = self._run(controller, inspect_stdout="true\n")

        self.assertEqual(
            docker_run_calls, [], "a healthy existing Redis container must not be recreated"
        )
        self.assertIn("localhost", controller.redis_containers)
        self.assertIn("localhost", controller.node_redis)

    def test_an_unhealthy_or_missing_container_still_gets_created(self):
        controller = _bare_controller(
            [{"name": "Workflow", "replicas": 1, "redis_port": 6379}]
        )

        docker_run_calls = self._run(controller, inspect_stdout="false\n")

        self.assertEqual(
            len(docker_run_calls), 1, "a not-running container must still be (re)created"
        )
        self.assertIn("localhost", controller.redis_containers)

    def test_reuse_check_probes_the_exact_expected_container_name(self):
        controller = _bare_controller(
            [{"name": "Workflow", "replicas": 1, "redis_port": 6379, "host": "10.0.0.5"}]
        )

        inspect_calls = []

        def fake_run_cmd(cmd, host, user=None):
            if cmd[:2] == ["docker", "inspect"]:
                inspect_calls.append(cmd)
                return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_cmd = fake_run_cmd

        with patch("canyonos_core.controller.global_controller.RedisClient") as fake_redis_cls, patch(
            "canyonos_core.controller.global_controller._wait_for_redis"
        ):
            fake_redis_cls.return_value = MagicMock()
            controller._launch_redis_containers()

        self.assertEqual(len(inspect_calls), 1)
        self.assertIn("canyonos-redis-10-0-0-5", inspect_calls[0])


if __name__ == "__main__":
    unittest.main()
