import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis import cli


class CliDeployTests(unittest.TestCase):
    def _fake_controller_module(self, controller):
        module = types.ModuleType("ventis.controller.global_controller")
        module.GlobalController = lambda _config_path: controller
        return module

    @patch("atexit.register")
    @patch("signal.signal")
    @patch("ventis.cli._ensure_grpc_stubs_importable")
    @patch("ventis.cli._preflight_ec2_deploy")
    def test_deploy_skips_ec2_preflight_for_local_config(
        self,
        preflight,
        ensure_grpc,
        _signal_patch,
        _atexit_patch,
    ):
        controller = MagicMock()
        controller_module = self._fake_controller_module(controller)
        args = SimpleNamespace(config="config/global_controller.yaml")
        config = {"agents": [{"name": "LocalAgent", "provider": "local"}]}

        with (
            patch("ventis.cli.os.path.isfile", return_value=True),
            patch("ventis.cli._load_config", return_value=config),
            patch.dict(sys.modules, {"ventis.controller.global_controller": controller_module}),
        ):
            cli.cmd_deploy(args)

        preflight.assert_not_called()
        ensure_grpc.assert_called_once_with(os.getcwd())
        controller.launch_docker_agents.assert_called_once_with()
        controller._wait_for_healthy.assert_called_once_with()
        controller.run.assert_called_once_with()

    @patch("atexit.register")
    @patch("signal.signal")
    @patch("ventis.cli._ensure_grpc_stubs_importable")
    @patch("ventis.cli._preflight_ec2_deploy")
    def test_deploy_runs_ec2_preflight_for_ec2_config(
        self,
        preflight,
        ensure_grpc,
        _signal_patch,
        _atexit_patch,
    ):
        controller = MagicMock()
        controller_module = self._fake_controller_module(controller)
        args = SimpleNamespace(config="config/global_controller.yaml")
        config = {"agents": [{"name": "Ec2Agent", "provider": "EC2"}]}

        with (
            patch("ventis.cli.os.path.isfile", return_value=True),
            patch("ventis.cli._load_config", return_value=config),
            patch.dict(sys.modules, {"ventis.controller.global_controller": controller_module}),
        ):
            cli.cmd_deploy(args)

        ensure_grpc.assert_called_once_with(os.getcwd())
        preflight.assert_called_once_with(config, os.getcwd())


class CliBuildTests(unittest.TestCase):
    def test_build_does_not_build_global_controller_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "config").mkdir()
            (project_dir / "agents").mkdir()
            (project_dir / "workflows").mkdir()
            (project_dir / "docker").mkdir()
            (project_dir / "docker" / "global-controller.Dockerfile").write_text("FROM scratch\n")
            (project_dir / "agents" / "example_agent.py").write_text("print('ok')\n")
            (project_dir / "workflows" / "example_workflow.py").write_text("print('ok')\n")
            agent_yaml = project_dir / "agents" / "example_agent.yaml"
            agent_yaml.write_text("agent:\n  name: ExampleAgent\n")
            config_path = project_dir / "config" / "global_controller.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "agents": [
                            {
                                "name": "ExampleAgent",
                                "entrypoint": "agents/example_agent.py",
                                "provider": "local",
                            },
                            {
                                "name": "Workflow",
                                "type": "workflow",
                                "workflow_file": "workflows/example_workflow.py",
                                "provider": "local",
                            },
                        ]
                    }
                )
            )

            args = SimpleNamespace(config=str(config_path))
            docker_calls = []

            def fake_run(cmd, check):
                docker_calls.append(cmd)
                return SimpleNamespace(returncode=0)

            with (
                patch("ventis.cli._get_package_dir", return_value=str(project_dir / "package")),
                patch("ventis.cli.glob.glob", side_effect=[[str(agent_yaml)], ["proto/a.proto"]]),
                patch("ventis.stub_generator.generate_stub"),
                patch("ventis.stub_generator.generate_docker"),
                patch("ventis.stub_generator.generate_workflow_docker"),
                patch("ventis.cli.subprocess.run", side_effect=fake_run),
                patch.dict(os.environ, {}, clear=False),
            ):
                cwd = os.getcwd()
                os.chdir(project_dir)
                try:
                    cli.cmd_build(args)
                finally:
                    os.chdir(cwd)

        flattened = [" ".join(call) for call in docker_calls]
        self.assertFalse(any("global-controller.Dockerfile" in call for call in flattened))
        self.assertEqual(sum(call[:2] == ["docker", "build"] for call in docker_calls), 2)


if __name__ == "__main__":
    unittest.main()
