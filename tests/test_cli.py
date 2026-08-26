import json
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
            patch.dict(
                sys.modules, {"ventis.controller.global_controller": controller_module}
            ),
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
            patch.dict(
                sys.modules, {"ventis.controller.global_controller": controller_module}
            ),
        ):
            cli.cmd_deploy(args)

        ensure_grpc.assert_called_once_with(os.getcwd())
        preflight.assert_called_once_with(config, os.getcwd())

    @patch("ventis.cli._ensure_grpc_stubs_importable")
    def test_deploy_exits_before_launching_when_env_file_is_missing(self, ensure_grpc):
        args = SimpleNamespace(config="config/global_controller.yaml")
        config = {
            "env_file": "definitely-not-here.env",
            "agents": [{"name": "LocalAgent", "provider": "local"}],
        }

        with (
            patch("ventis.cli.os.path.isfile", return_value=True),
            patch("ventis.cli._load_config", return_value=config),
            self.assertRaises(SystemExit) as exit_ctx,
        ):
            cli.cmd_deploy(args)

        self.assertEqual(exit_ctx.exception.code, 1)
        ensure_grpc.assert_not_called()

    @patch("ventis.cli._ensure_grpc_stubs_importable")
    @patch("ventis.cli._require_docker_for_ec2")
    def test_preflight_does_not_require_ssh_fields(self, require_docker, ensure_grpc):
        config = {
            "ec2": {
                "ami_id": "ami-123",
                "subnet_id": "subnet-123",
                "security_group_ids": ["sg-123"],
                "region": "us-east-1",
            }
        }

        cli._preflight_ec2_deploy(config, os.getcwd())

        require_docker.assert_called_once_with("deploy")
        ensure_grpc.assert_called_once_with(os.getcwd())


class CliBuildTests(unittest.TestCase):
    def _run_build(
        self, project_dir, agent_yaml_paths, buildx_available, platform="linux/amd64"
    ):
        """Run cmd_build against project_dir with docker/subprocess calls mocked.

        Returns (docker_calls, generate_docker_mock, generate_workflow_docker_mock).
        """
        config_path = project_dir / "config" / "global_controller.yaml"
        args = SimpleNamespace(config=str(config_path))
        docker_calls = []

        def fake_run(cmd, check):
            docker_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with (
            patch(
                "ventis.cli._get_package_dir",
                return_value=str(project_dir / "package"),
            ),
            patch(
                "ventis.cli.glob.glob",
                side_effect=[agent_yaml_paths, ["proto/a.proto"]],
            ),
            patch("ventis.stub_generator.generate_stub"),
            patch("ventis.stub_generator.generate_docker") as generate_docker,
            patch(
                "ventis.stub_generator.generate_workflow_docker"
            ) as generate_workflow_docker,
            patch("ventis.cli.subprocess.run", side_effect=fake_run),
            patch("ventis.cli._docker_available", return_value=buildx_available),
            patch("ventis.cli._docker_platform", return_value=platform),
        ):
            cwd = os.getcwd()
            os.chdir(project_dir)
            try:
                cli.cmd_build(args)
            finally:
                os.chdir(cwd)

        return docker_calls, generate_docker, generate_workflow_docker

    def _write_agent_and_workflow_config(self, project_dir):
        """Scaffold a project with one agent + one workflow entry; returns the agent YAML path."""
        (project_dir / "config").mkdir()
        (project_dir / "agents").mkdir()
        (project_dir / "workflows").mkdir()
        (project_dir / "docker").mkdir()
        (project_dir / "docker" / "global-controller.Dockerfile").write_text(
            "FROM scratch\n"
        )
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
        return agent_yaml

    def test_build_falls_back_to_sequential_docker_build_without_buildx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            agent_yaml = self._write_agent_and_workflow_config(project_dir)

            docker_calls, _, _ = self._run_build(
                project_dir, [str(agent_yaml)], buildx_available=False
            )

        flattened = [" ".join(call) for call in docker_calls]
        self.assertFalse(
            any("global-controller.Dockerfile" in call for call in flattened)
        )
        self.assertEqual(
            sum(call[:2] == ["docker", "build"] for call in docker_calls), 2
        )
        self.assertFalse(
            any(call[:3] == ["docker", "buildx", "bake"] for call in docker_calls)
        )

    def test_build_uses_buildx_bake_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            agent_yaml = self._write_agent_and_workflow_config(project_dir)

            docker_calls, _, _ = self._run_build(
                project_dir, [str(agent_yaml)], buildx_available=True
            )

            bake_file = project_dir / "docker_container" / "docker-bake.json"
            self.assertTrue(bake_file.is_file())
            with open(bake_file) as f:
                bake_config = json.load(f)

        self.assertEqual(
            sum(call[:2] == ["docker", "build"] for call in docker_calls), 0
        )
        bake_calls = [
            call for call in docker_calls if call[:3] == ["docker", "buildx", "bake"]
        ]
        self.assertEqual(len(bake_calls), 1)
        self.assertIn("--file", bake_calls[0])
        self.assertEqual(
            os.path.realpath(bake_calls[0][bake_calls[0].index("--file") + 1]),
            os.path.realpath(bake_file),
        )

        targets = bake_config["target"]
        self.assertEqual(len(targets), 2)
        self.assertEqual(
            os.path.realpath(targets["exampleagent"]["context"]),
            os.path.realpath(project_dir / "docker_container" / "ExampleAgent"),
        )
        self.assertTrue(os.path.isabs(targets["exampleagent"]["context"]))
        self.assertEqual(targets["exampleagent"]["tags"], ["ventis-exampleagent"])
        self.assertEqual(targets["exampleagent"]["platforms"], ["linux/amd64"])
        self.assertEqual(targets["exampleagent"]["output"], ["type=docker"])
        self.assertEqual(
            os.path.realpath(targets["workflow"]["context"]),
            os.path.realpath(project_dir / "docker_container" / "Workflow"),
        )
        self.assertEqual(targets["workflow"]["tags"], ["ventis-workflow"])

    def test_build_with_no_agents_builds_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "config").mkdir()
            (project_dir / "agents").mkdir()
            config_path = project_dir / "config" / "global_controller.yaml"
            config_path.write_text(yaml.safe_dump({"agents": []}))

            docker_calls, _, _ = self._run_build(project_dir, [], buildx_available=True)

        self.assertFalse(any(call[0] == "docker" for call in docker_calls))

    def test_build_skips_agent_without_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "config").mkdir()
            (project_dir / "agents").mkdir()
            config_path = project_dir / "config" / "global_controller.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "agents": [
                            {"name": "NoEntrypointAgent", "provider": "local"},
                        ]
                    }
                )
            )

            docker_calls, _, _ = self._run_build(project_dir, [], buildx_available=True)

        self.assertFalse(any(call[0] == "docker" for call in docker_calls))

    def _write_requirements_config(self, project_dir):
        """Scaffold one plain agent, one agent with `requirements`, one workflow with `requirements`."""
        (project_dir / "config").mkdir()
        (project_dir / "agents").mkdir()
        (project_dir / "workflows").mkdir()
        (project_dir / "docker").mkdir()
        (project_dir / "docker" / "global-controller.Dockerfile").write_text(
            "FROM scratch\n"
        )
        (project_dir / "agents" / "example_agent.py").write_text("print('ok')\n")
        (project_dir / "agents" / "vllm_agent.py").write_text("print('ok')\n")
        (project_dir / "workflows" / "example_workflow.py").write_text("print('ok')\n")

        example_yaml = project_dir / "agents" / "example_agent.yaml"
        example_yaml.write_text("agent:\n  name: ExampleAgent\n")
        vllm_yaml = project_dir / "agents" / "vllm_agent.yaml"
        vllm_yaml.write_text("agent:\n  name: VllmAgent\n")

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
                            "name": "VllmAgent",
                            "entrypoint": "agents/vllm_agent.py",
                            "provider": "local",
                            "requirements": ["yfinance"],
                        },
                        {
                            "name": "Workflow",
                            "type": "workflow",
                            "workflow_file": "workflows/example_workflow.py",
                            "provider": "local",
                            "requirements": ["sqlalchemy-utils"],
                        },
                    ]
                }
            )
        )
        return [str(example_yaml), str(vllm_yaml)]

    def test_build_passes_per_agent_requirements_to_generators(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            agent_yamls = self._write_requirements_config(project_dir)

            _, generate_docker, generate_workflow_docker = self._run_build(
                project_dir, agent_yamls, buildx_available=True
            )

        requirements_by_agent = {
            os.path.basename(call.kwargs["output_dir"]): call.kwargs["requirements"]
            for call in generate_docker.call_args_list
        }
        self.assertEqual(requirements_by_agent["ExampleAgent"], [])
        self.assertEqual(requirements_by_agent["VllmAgent"], ["yfinance"])

        self.assertEqual(
            generate_workflow_docker.call_args.kwargs["requirements"],
            ["sqlalchemy-utils"],
        )

    def test_build_ignores_non_list_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "config").mkdir()
            (project_dir / "agents").mkdir()
            (project_dir / "agents" / "example_agent.py").write_text("print('ok')\n")
            example_yaml = project_dir / "agents" / "example_agent.yaml"
            example_yaml.write_text("agent:\n  name: ExampleAgent\n")
            config_path = project_dir / "config" / "global_controller.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "agents": [
                            {
                                "name": "ExampleAgent",
                                "entrypoint": "agents/example_agent.py",
                                "provider": "local",
                                "requirements": "boto3",
                            },
                        ]
                    }
                )
            )

            with self.assertLogs("ventis", level="WARNING") as log:
                _, generate_docker, _ = self._run_build(
                    project_dir, [str(example_yaml)], buildx_available=True
                )

            self.assertIn("requirements", log.output[0])
            self.assertEqual(generate_docker.call_args.kwargs["requirements"], [])


if __name__ == "__main__":
    unittest.main()
