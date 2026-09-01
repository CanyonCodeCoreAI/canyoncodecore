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
        ensure_grpc.assert_called_once_with(os.path.join(os.getcwd(), ".car"))
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

        ensure_grpc.assert_called_once_with(os.path.join(os.getcwd(), ".car"))
        preflight.assert_called_once_with(config)
        controller.run.assert_called_once_with()

    @patch("ventis.cli._require_docker_for_ec2")
    def test_preflight_does_not_require_ssh_fields(self, require_docker):
        config = {
            "ec2": {
                "ami_id": "ami-123",
                "subnet_id": "subnet-123",
                "security_group_ids": ["sg-123"],
                "region": "us-east-1",
            }
        }

        cli._preflight_ec2_deploy(config)

        require_docker.assert_called_once_with("deploy")


class CliBuildTests(unittest.TestCase):
    """Build runs from the application root and reads `.car` below it.

    Each test scaffolds that `.car` -- `config/` beside the `app/` copy of the
    application source -- and runs the command from the directory holding it.
    """

    def _run_build(self, artifact_root, buildx_available, platform="linux/amd64"):
        """Run cmd_build against an artifact root with docker/subprocess calls mocked.

        Returns (docker_calls, generate_stub, generate_docker, generate_workflow_docker).
        """
        args = SimpleNamespace(config=cli.DEFAULT_CONFIG_PATH)
        docker_calls = []

        # Discovery globs config/ for declarations, so let glob run for real and
        # give the proto compile step something to find instead.
        proto_dir = artifact_root / "package" / "controller" / "proto"
        proto_dir.mkdir(parents=True, exist_ok=True)
        (proto_dir / "local_controler.proto").write_text("syntax = \"proto3\";\n")

        def fake_run(cmd, check):
            docker_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with (
            patch(
                "ventis.cli._get_package_dir",
                return_value=str(artifact_root / "package"),
            ),
            patch("ventis.stub_generator.generate_stub") as generate_stub,
            patch("ventis.stub_generator.generate_docker") as generate_docker,
            patch(
                "ventis.stub_generator.generate_workflow_docker"
            ) as generate_workflow_docker,
            patch("ventis.cli.subprocess.run", side_effect=fake_run),
            patch("ventis.cli._docker_available", return_value=buildx_available),
            patch("ventis.cli._docker_platform", return_value=platform),
        ):
            cwd = os.getcwd()
            os.chdir(artifact_root.parent)
            try:
                cli.cmd_build(args)
            finally:
                os.chdir(cwd)

        return docker_calls, generate_stub, generate_docker, generate_workflow_docker

    def _write_config(self, artifact_root, config):
        config_dir = artifact_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (artifact_root / "app").mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "global_controller.yaml"
        config_path.write_text(yaml.safe_dump(config))
        return config_path

    def _write_source(self, artifact_root, rel_path, content="print('ok')\n"):
        path = artifact_root / "app" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def _write_declaration(self, artifact_root, fname, agent_name):
        """Agent declarations live in config/, beside the manifest."""
        config_dir = artifact_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / fname
        path.write_text(f"agent:\n  name: {agent_name}\n")
        return path

    def _write_agent_and_workflow_config(self, artifact_root):
        """Scaffold one agent + one workflow, each in its own source directory."""
        self._write_source(artifact_root, "src/joke/agent.py")
        self._write_source(artifact_root, "src/flows/joke_flow.py")
        agent_yaml = self._write_declaration(
            artifact_root, "example_agent.yaml", "ExampleAgent"
        )
        self._write_config(
            artifact_root,
            {
                "agents": [
                    {
                        "name": "ExampleAgent",
                        "entrypoint": "src/joke/agent.py",
                        "provider": "local",
                    },
                    {
                        "name": "Workflow",
                        "type": "workflow",
                        "workflow_file": "src/flows/joke_flow.py",
                        "provider": "local",
                    },
                ]
            },
        )
        return agent_yaml

    def test_build_falls_back_to_sequential_docker_build_without_buildx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_agent_and_workflow_config(artifact_root)

            docker_calls, _, _, _ = self._run_build(
                artifact_root, buildx_available=False
            )

        self.assertEqual(
            sum(call[:2] == ["docker", "build"] for call in docker_calls), 2
        )
        self.assertFalse(
            any(call[:3] == ["docker", "buildx", "bake"] for call in docker_calls)
        )

    def test_build_uses_buildx_bake_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_agent_and_workflow_config(artifact_root)

            docker_calls, _, _, _ = self._run_build(
                artifact_root, buildx_available=True
            )

            bake_file = artifact_root / "docker_container" / "docker-bake.json"
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
            os.path.realpath(artifact_root / "docker_container" / "ExampleAgent"),
        )
        self.assertTrue(os.path.isabs(targets["exampleagent"]["context"]))
        self.assertEqual(targets["exampleagent"]["tags"], ["ventis-exampleagent"])
        self.assertEqual(targets["exampleagent"]["platforms"], ["linux/amd64"])
        self.assertEqual(targets["exampleagent"]["output"], ["type=docker"])
        self.assertEqual(
            os.path.realpath(targets["workflow"]["context"]),
            os.path.realpath(artifact_root / "docker_container" / "Workflow"),
        )
        self.assertEqual(targets["workflow"]["tags"], ["ventis-workflow"])

    def test_build_generates_contexts_from_the_source_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_agent_and_workflow_config(artifact_root)

            _, _, generate_docker, generate_workflow_docker = self._run_build(
                artifact_root, buildx_available=True
            )

            source_root = os.path.realpath(artifact_root / "app")
            self.assertEqual(
                os.path.realpath(generate_docker.call_args.kwargs["project_dir"]),
                source_root,
            )
            self.assertEqual(
                os.path.realpath(
                    generate_workflow_docker.call_args.kwargs["project_dir"]
                ),
                source_root,
            )
            self.assertEqual(
                generate_docker.call_args.kwargs["agent_entrypoint"],
                "src/joke/agent.py",
            )
            self.assertEqual(
                generate_workflow_docker.call_args.kwargs["workflow_entrypoint"],
                "src/flows/joke_flow.py",
            )
            self.assertEqual(
                os.path.realpath(generate_docker.call_args.args[1]),
                os.path.realpath(artifact_root / "app" / "src" / "joke" / "agent.py"),
            )

    def test_stub_lands_on_the_module_the_caller_already_imports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_agent_and_workflow_config(artifact_root)

            _, generate_stub, _, generate_workflow_docker = self._run_build(
                artifact_root, buildx_available=True
            )

            self.assertEqual(
                os.path.realpath(generate_stub.call_args.args[1]),
                os.path.realpath(
                    artifact_root / "stubs" / "src" / "joke" / "agent.py"
                ),
            )
            self.assertEqual(
                [
                    dest
                    for _, dest in generate_workflow_docker.call_args.args[1]
                ],
                ["src/joke/agent.py"],
            )

    def test_agent_context_excludes_the_agent_own_stub(self):
        """An agent's own stub would replace the implementation it has to run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_source(artifact_root, "src/a/agent.py")
            self._write_declaration(artifact_root, "a.yaml", "AgentA")
            self._write_source(artifact_root, "src/b/agent.py")
            self._write_declaration(artifact_root, "b.yaml", "AgentB")
            self._write_config(
                artifact_root,
                {
                    "agents": [
                        {
                            "name": "AgentA",
                            "entrypoint": "src/a/agent.py",
                            "provider": "local",
                        },
                        {
                            "name": "AgentB",
                            "entrypoint": "src/b/agent.py",
                            "provider": "local",
                        },
                    ]
                },
            )

            _, _, generate_docker, _ = self._run_build(
                artifact_root, buildx_available=True
            )

        stubs_by_agent = {
            os.path.basename(call.kwargs["output_dir"]): [
                dest for _, dest in call.kwargs["stub_files"]
            ]
            for call in generate_docker.call_args_list
        }
        self.assertEqual(stubs_by_agent["AgentA"], ["src/b/agent.py"])
        self.assertEqual(stubs_by_agent["AgentB"], ["src/a/agent.py"])

    def test_build_fails_on_an_entry_it_cannot_build(self):
        """A missing image must fail here, not at `docker run` two steps later."""
        entrypoint = {
            "name": "ExampleAgent",
            "entrypoint": "src/joke/agent.py",
            "provider": "local",
        }
        cases = {
            # (write the module, write the declaration, config entry)
            "no declaration": (True, False, entrypoint),
            "entrypoint missing from the copy": (False, True, entrypoint),
            "no entrypoint": (
                False,
                True,
                {"name": "ExampleAgent", "provider": "local"},
            ),
        }

        for case, (write_module, write_declaration, entry) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                artifact_root = Path(tmpdir) / ".car"
                if write_module:
                    self._write_source(artifact_root, "src/joke/agent.py")
                if write_declaration:
                    self._write_declaration(
                        artifact_root, "example_agent.yaml", "ExampleAgent"
                    )
                self._write_config(artifact_root, {"agents": [entry]})

                with self.assertLogs("ventis", level="ERROR") as log, self.assertRaises(
                    SystemExit
                ):
                    self._run_build(artifact_root, buildx_available=True)

                self.assertIn("ExampleAgent", log.output[-1])

    def test_build_reads_only_declarations_out_of_config(self):
        """The manifest and policy.yaml share the directory and declare no agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_agent_and_workflow_config(artifact_root)
            (artifact_root / "config" / "policy.yaml").write_text(
                yaml.safe_dump({"rules": [{"service": "ExampleAgent"}]})
            )

            _, generate_stub, _, _ = self._run_build(
                artifact_root, buildx_available=True
            )

        self.assertEqual(generate_stub.call_count, 1)

    def test_build_rejects_an_entrypoint_outside_the_source_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_declaration(artifact_root, "example_agent.yaml", "ExampleAgent")
            self._write_config(
                artifact_root,
                {
                    "agents": [
                        {
                            "name": "ExampleAgent",
                            "entrypoint": "../../elsewhere/agent.py",
                            "provider": "local",
                        }
                    ]
                },
            )

            with self.assertLogs("ventis", level="ERROR"), self.assertRaises(SystemExit):
                self._run_build(artifact_root, buildx_available=True)

    def test_build_without_a_source_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            (artifact_root / "config").mkdir(parents=True)
            (artifact_root / "config" / "global_controller.yaml").write_text(
                yaml.safe_dump({"agents": []})
            )

            with self.assertLogs("ventis", level="ERROR"), self.assertRaises(
                SystemExit
            ):
                self._run_build(artifact_root, buildx_available=True)

    def test_build_with_no_agents_builds_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_config(artifact_root, {"agents": []})

            docker_calls, _, _, _ = self._run_build(
                artifact_root, buildx_available=True
            )

        self.assertFalse(any(call[0] == "docker" for call in docker_calls))

    def _write_requirements_config(self, artifact_root):
        """Scaffold one plain agent, one agent with `requirements`, one workflow with `requirements`."""
        self._write_source(artifact_root, "src/joke/agent.py")
        self._write_declaration(artifact_root, "example_agent.yaml", "ExampleAgent")
        self._write_source(artifact_root, "src/vllm/agent.py")
        self._write_declaration(artifact_root, "vllm_agent.yaml", "VllmAgent")
        self._write_source(artifact_root, "src/flows/joke_flow.py")
        self._write_config(
            artifact_root,
            {
                "agents": [
                    {
                        "name": "ExampleAgent",
                        "entrypoint": "src/joke/agent.py",
                        "provider": "local",
                    },
                    {
                        "name": "VllmAgent",
                        "entrypoint": "src/vllm/agent.py",
                        "provider": "local",
                        "requirements": ["yfinance"],
                    },
                    {
                        "name": "Workflow",
                        "type": "workflow",
                        "workflow_file": "src/flows/joke_flow.py",
                        "provider": "local",
                        "requirements": ["sqlalchemy-utils"],
                    },
                ]
            },
        )

    def test_build_passes_per_agent_requirements_to_generators(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_root = Path(tmpdir) / ".car"
            self._write_requirements_config(artifact_root)

            _, _, generate_docker, generate_workflow_docker = self._run_build(
                artifact_root, buildx_available=True
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
            artifact_root = Path(tmpdir) / ".car"
            self._write_source(artifact_root, "src/joke/agent.py")
            self._write_declaration(artifact_root, "example_agent.yaml", "ExampleAgent")
            self._write_config(
                artifact_root,
                {
                    "agents": [
                        {
                            "name": "ExampleAgent",
                            "entrypoint": "src/joke/agent.py",
                            "provider": "local",
                            "requirements": "boto3",
                        }
                    ]
                },
            )

            with self.assertLogs("ventis", level="WARNING") as log:
                _, _, generate_docker, _ = self._run_build(
                    artifact_root, buildx_available=True
                )

            self.assertIn("requirements", log.output[0])
            self.assertEqual(generate_docker.call_args.kwargs["requirements"], [])


if __name__ == "__main__":
    unittest.main()
