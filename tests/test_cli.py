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

from canyonos_core import cli


class CliDeployTests(unittest.TestCase):
    def _fake_controller_module(self, controller):
        module = types.ModuleType("canyonos_core.controller.global_controller")
        module.GlobalController = lambda _config_path: controller
        return module

    @patch("atexit.register")
    @patch("signal.signal")
    @patch("canyonos_core.cli._run_build")
    @patch("canyonos_core.cli._ensure_grpc_stubs_importable")
    @patch("canyonos_core.cli._preflight_ec2_deploy")
    def test_deploy_skips_ec2_preflight_for_local_config(
        self,
        preflight,
        ensure_grpc,
        _run_build,
        _signal_patch,
        _atexit_patch,
    ):
        controller = MagicMock()
        controller_module = self._fake_controller_module(controller)
        args = SimpleNamespace(config="config/global_controller.yaml")
        config = {"agents": [{"name": "LocalAgent", "provider": "local"}]}

        with (
            patch("canyonos_core.cli.os.path.isfile", return_value=True),
            patch("canyonos_core.cli._load_config", return_value=config),
            patch.dict(
                sys.modules, {"canyonos_core.controller.global_controller": controller_module}
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
    @patch("canyonos_core.cli._run_build")
    @patch("canyonos_core.cli._ensure_grpc_stubs_importable")
    @patch("canyonos_core.cli._preflight_ec2_deploy")
    def test_deploy_runs_ec2_preflight_for_ec2_config(
        self,
        preflight,
        ensure_grpc,
        _run_build,
        _signal_patch,
        _atexit_patch,
    ):
        controller = MagicMock()
        controller_module = self._fake_controller_module(controller)
        args = SimpleNamespace(config="config/global_controller.yaml")
        config = {"agents": [{"name": "Ec2Agent", "provider": "EC2"}]}

        with (
            patch("canyonos_core.cli.os.path.isfile", return_value=True),
            patch("canyonos_core.cli._load_config", return_value=config),
            patch.dict(
                sys.modules, {"canyonos_core.controller.global_controller": controller_module}
            ),
        ):
            cli.cmd_deploy(args)

        ensure_grpc.assert_called_once_with(os.getcwd())
        preflight.assert_called_once_with(config, os.getcwd())
        controller.run.assert_called_once_with()

    @patch("atexit.register")
    @patch("signal.signal")
    @patch("canyonos_core.cli._run_build")
    @patch("canyonos_core.cli._ensure_grpc_stubs_importable")
    @patch("canyonos_core.cli._preflight_ec2_deploy")
    def test_deploy_uses_car_when_present(
        self, preflight, ensure_grpc, _run_build, _signal_patch, _atexit_patch
    ):
        controller = MagicMock()
        controller_module = self._fake_controller_module(controller)
        args = SimpleNamespace(config=".car/config/global_controller.yaml")

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "canyonos_core.cli.os.path.isfile", return_value=True
        ), patch(
            "canyonos_core.cli._load_config", return_value={"agents": []}
        ), patch.dict(
            sys.modules, {"canyonos_core.controller.global_controller": controller_module}
        ):
            Path(tmpdir, ".car").mkdir()
            cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                cli.cmd_deploy(args)
            finally:
                os.chdir(cwd)

        ensure_grpc.assert_called_once_with(os.path.join(os.path.realpath(tmpdir), ".car"))
        preflight.assert_not_called()

    @patch("canyonos_core.cli._ensure_grpc_stubs_importable")
    @patch("canyonos_core.cli._require_docker_for_ec2")
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
        """Run _run_build against project_dir with docker/subprocess calls mocked.

        Returns (docker_calls, generate_docker_mock, generate_workflow_docker_mock).
        """
        artifact_root = (
            project_dir / ".car" if (project_dir / ".car").is_dir() else project_dir
        )
        config_path = artifact_root / "config" / "global_controller.yaml"
        docker_calls = []

        def fake_run(cmd, check):
            docker_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        def fake_glob(pattern):
            if pattern.endswith("*.proto"):
                return ["proto/a.proto"]
            if agent_yaml_paths:
                self.assertEqual(
                    os.path.realpath(Path(pattern).parent),
                    os.path.realpath(Path(agent_yaml_paths[0]).parent),
                )
            return agent_yaml_paths

        def fake_generate_stub(yaml_path, _output_path):
            with open(yaml_path) as f:
                self.assertIn("agent", yaml.safe_load(f))

        with (
            patch(
                "canyonos_core.cli._get_package_dir",
                return_value=str(project_dir / "package"),
            ),
            patch("canyonos_core.cli.glob.glob", side_effect=fake_glob),
            patch(
                "canyonos_core.stub_generator.generate_stub", side_effect=fake_generate_stub
            ),
            patch("canyonos_core.stub_generator.generate_docker") as generate_docker,
            patch(
                "canyonos_core.stub_generator.generate_workflow_docker"
            ) as generate_workflow_docker,
            patch("canyonos_core.cli.subprocess.run", side_effect=fake_run),
            patch("canyonos_core.cli._docker_available", return_value=buildx_available),
            patch("canyonos_core.cli._docker_platform", return_value=platform),
        ):
            cwd = os.getcwd()
            os.chdir(project_dir)
            try:
                cli._run_build(str(config_path))
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
        self.assertEqual(targets["exampleagent"]["tags"], ["canyonos-exampleagent"])
        self.assertEqual(targets["exampleagent"]["platforms"], ["linux/amd64"])
        self.assertEqual(targets["exampleagent"]["output"], ["type=docker"])
        self.assertEqual(
            os.path.realpath(targets["workflow"]["context"]),
            os.path.realpath(project_dir / "docker_container" / "Workflow"),
        )
        self.assertEqual(targets["workflow"]["tags"], ["canyonos-workflow"])

    def test_build_uses_car_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            artifact_root = project_dir / ".car"
            source_root = artifact_root / "app"
            source_root.mkdir(parents=True)
            source_yaml = self._write_agent_and_workflow_config(source_root)
            source_root.joinpath("config").rename(artifact_root / "config")
            agent_yaml = artifact_root / "config" / source_yaml.name
            source_yaml.rename(agent_yaml)

            manifest = artifact_root / "config" / "global_controller.yaml"
            _, generate_docker, generate_workflow_docker = self._run_build(
                project_dir, [str(manifest), str(agent_yaml)], buildx_available=True
            )

        for call in (generate_docker, generate_workflow_docker):
            self.assertEqual(
                os.path.realpath(call.call_args.kwargs["project_dir"]),
                os.path.realpath(source_root),
            )
        self.assertEqual(
            os.path.realpath(generate_docker.call_args.kwargs["output_dir"]),
            os.path.realpath(artifact_root / "docker_container" / "ExampleAgent"),
        )

    def test_build_with_no_agents_builds_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "config").mkdir()
            (project_dir / "agents").mkdir()
            config_path = project_dir / "config" / "global_controller.yaml"
            config_path.write_text(yaml.safe_dump({"agents": []}))

            docker_calls, _, _ = self._run_build(project_dir, [], buildx_available=True)

        self.assertFalse(any(call[0] == "docker" for call in docker_calls))

    def test_build_fails_when_stub_cannot_be_generated(self):
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

            with self.assertRaises(SystemExit):
                self._run_build(project_dir, [], buildx_available=True)

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

            with self.assertLogs("canyonos_core", level="WARNING") as log:
                _, generate_docker, _ = self._run_build(
                    project_dir, [str(example_yaml)], buildx_available=True
                )

            self.assertIn("requirements", log.output[0])
            self.assertEqual(generate_docker.call_args.kwargs["requirements"], [])


class CliCleanTests(unittest.TestCase):
    def test_clean_uses_car_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / ".car" / "stubs").mkdir(parents=True)
            (project_dir / "stubs").mkdir()
            cwd = os.getcwd()
            os.chdir(project_dir)
            try:
                cli.cmd_clean(SimpleNamespace())
            finally:
                os.chdir(cwd)

            self.assertFalse((project_dir / ".car" / "stubs").exists())
            self.assertTrue((project_dir / "stubs").exists())


if __name__ == "__main__":
    unittest.main()
