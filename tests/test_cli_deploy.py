import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from ventis import cli


class _FakeController:
    def __init__(self, config_path):
        self.config_path = config_path
        self.cleanup_calls = 0
        self.launch_calls = 0
        self.wait_calls = 0
        self.run_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1

    def launch_agents(self):
        self.launch_calls += 1

    def _wait_for_healthy(self):
        self.wait_calls += 1

    def run(self):
        self.run_calls += 1


class CliDeployTests(unittest.TestCase):
    def setUp(self):
        self._repo_cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        os.chdir(self._repo_cwd)

    def _write_config(self, root, *, provider="local", include_ec2=False, ssh_key_path=None):
        config_dir = os.path.join(root, "config")
        grpc_dir = os.path.join(root, "grpc_stubs")
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(grpc_dir, exist_ok=True)

        with open(os.path.join(grpc_dir, "local_controler_pb2.py"), "w") as f:
            f.write("MESSAGE = 'ok'\n")
        with open(os.path.join(grpc_dir, "local_controler_pb2_grpc.py"), "w") as f:
            f.write("SERVICE = 'ok'\n")

        config = {
            "agents": [{"name": "ExampleAgent", "provider": provider}],
            "redis": {"host": "localhost", "port": 6379, "db": 0},
        }
        if include_ec2:
            config["ec2"] = {
                "region": "us-east-1",
                "ami_id": "ami-123",
                "instance_type": "t2.nano",
                "subnet_id": "subnet-123",
                "security_group_ids": ["sg-123"],
                "ssh_user": "ec2-user",
            }
            if ssh_key_path is not None:
                config["ec2"]["ssh_private_key_path"] = ssh_key_path

        with open(os.path.join(config_dir, "global_controller.yaml"), "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)

    def test_ec2_deploy_uses_global_controller_config_and_runs_controller(self):
        created = []

        def _factory(config_path):
            controller = _FakeController(config_path)
            created.append(controller)
            return controller

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir, provider="EC2", include_ec2=True)
            args = SimpleNamespace(config="config/global_controller.yaml")
            os.chdir(tmpdir)
            try:
                with patch("ventis.controller.global_controller.GlobalController", side_effect=_factory):
                    with patch("ventis.cli._running_on_ec2", return_value=True):
                        with patch("ventis.cli._docker_available", return_value=True):
                            cli.cmd_deploy(args)
            finally:
                os.chdir(self._repo_cwd)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].config_path, "config/global_controller.yaml")
        self.assertEqual(created[0].launch_calls, 1)
        self.assertEqual(created[0].wait_calls, 1)
        self.assertEqual(created[0].run_calls, 1)

    def test_ec2_deploy_preflight_stops_before_controller_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_key = os.path.join(tmpdir, "missing.pem")
            self._write_config(tmpdir, provider="EC2", include_ec2=True, ssh_key_path=missing_key)
            args = SimpleNamespace(config="config/global_controller.yaml")
            os.chdir(tmpdir)
            try:
                with patch("ventis.controller.global_controller.GlobalController") as controller_cls:
                    with patch("ventis.cli._running_on_ec2", return_value=True):
                        with patch("ventis.cli._docker_available", return_value=True):
                            with self.assertRaisesRegex(RuntimeError, "ssh_private_key_path does not exist"):
                                cli.cmd_deploy(args)
            finally:
                os.chdir(self._repo_cwd)

        controller_cls.assert_not_called()

    def test_non_ec2_deploy_keeps_existing_behavior(self):
        created = []

        def _factory(config_path):
            controller = _FakeController(config_path)
            created.append(controller)
            return controller

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_config(tmpdir, provider="local", include_ec2=False)
            args = SimpleNamespace(config="config/global_controller.yaml")
            os.chdir(tmpdir)
            try:
                with patch("ventis.controller.global_controller.GlobalController", side_effect=_factory):
                    with patch("ventis.cli._running_on_ec2", return_value=False):
                        cli.cmd_deploy(args)
            finally:
                os.chdir(self._repo_cwd)

        self.assertEqual(created[0].config_path, "config/global_controller.yaml")
        self.assertEqual(created[0].launch_calls, 1)
        self.assertEqual(created[0].wait_calls, 1)
        self.assertEqual(created[0].run_calls, 1)


if __name__ == "__main__":
    unittest.main()
