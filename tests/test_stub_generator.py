import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core import stub_generator
from canyonos_core.stub_generator import (
    BASE_AGENT_REQUIREMENTS,
    BASE_WORKFLOW_REQUIREMENTS,
    PLATFORM_PINS,
    _stub_destination,
    _sweep_project_files,
    generate_docker,
    generate_workflow_docker,
)


def _read_requirements(output_dir):
    return (Path(output_dir) / "requirements.txt").read_text().splitlines()


def _read_dockerfile(output_dir):
    return (Path(output_dir) / "Dockerfile").read_text()


class GenerateDockerRequirementsTests(unittest.TestCase):
    def _write_agent_yaml(self, tmpdir, name="ExampleAgent"):
        yaml_path = Path(tmpdir) / f"{name}.yaml"
        yaml_path.write_text(yaml.safe_dump({"agent": {"name": name}}))
        agent_file = Path(tmpdir) / "agent.py"
        agent_file.write_text("print('ok')\n")
        return str(yaml_path), str(agent_file)

    def test_base_only_when_requirements_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path, agent_file = self._write_agent_yaml(tmpdir)
            output_dir = os.path.join(tmpdir, "out")

            generate_docker(yaml_path, agent_file, output_dir=output_dir)

            requirements = _read_requirements(output_dir)

        self.assertEqual(requirements, BASE_AGENT_REQUIREMENTS)
        self.assertEqual(
            requirements,
            [
                "grpcio==1.83.1",
                "grpcio-tools==1.65.5",
                "protobuf==5.29.6",
                "redis==8.1.0",
                "pyyaml==6.0.3",
                "psutil==7.2.2",
                "boto3==1.43.91",
                "flask==3.1.3",
                "requests==2.34.2",
            ],
        )
        self.assertNotIn("yfinance", requirements)

    def test_per_agent_requirements_are_appended_to_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path, agent_file = self._write_agent_yaml(tmpdir)
            output_dir = os.path.join(tmpdir, "out")

            generate_docker(
                yaml_path, agent_file, output_dir=output_dir, requirements=["yfinance"]
            )

            requirements = _read_requirements(output_dir)

        self.assertEqual(requirements, BASE_AGENT_REQUIREMENTS + ["yfinance"])


class GenerateWorkflowDockerRequirementsTests(unittest.TestCase):
    def _write_workflow_file(self, tmpdir):
        workflow_file = Path(tmpdir) / "workflow.py"
        workflow_file.write_text("print('ok')\n")
        return str(workflow_file)

    def test_base_only_when_requirements_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_file = self._write_workflow_file(tmpdir)
            output_dir = os.path.join(tmpdir, "out")

            generate_workflow_docker(workflow_file, [], output_dir=output_dir)

            requirements = _read_requirements(output_dir)

        self.assertEqual(requirements, BASE_WORKFLOW_REQUIREMENTS)
        self.assertNotIn("yfinance", requirements)

    def test_per_workflow_requirements_are_appended_to_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_file = self._write_workflow_file(tmpdir)
            output_dir = os.path.join(tmpdir, "out")

            generate_workflow_docker(
                workflow_file, [], output_dir=output_dir, requirements=["yfinance"]
            )

            requirements = _read_requirements(output_dir)

        self.assertEqual(requirements, BASE_WORKFLOW_REQUIREMENTS + ["yfinance"])


class GenerateWorkflowDockerLauncherTests(unittest.TestCase):
    def test_launcher_reports_ready_only_after_the_workflow_port_accepts_connections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_file = Path(tmpdir) / "workflow.py"
            workflow_file.write_text("raise RuntimeError('broken')\n")
            output_dir = os.path.join(tmpdir, "out")

            generate_workflow_docker(
                str(workflow_file), [], output_dir=output_dir, api_port=9123
            )

            launcher = (Path(output_dir) / "workflow_launcher.py").read_text()

        self.assertIn(
            "controller = LocalController(port=50051, publish_ready=False)", launcher
        )
        self.assertIn("target=controller.run", launcher)
        self.assertIn(
            'socket.create_connection(("127.0.0.1", 9123)', launcher
        )
        self.assertIn("controller.mark_ready()", launcher)
        self.assertIn("except Exception:", launcher)
        self.assertIn("controller.mark_failed()", launcher)
        self.assertIn("traceback.print_exc()", launcher)
        self.assertIn("sys.exit(1)", launcher)


def _write(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class ProjectSweepTests(unittest.TestCase):
    """The sweep carries the whole project, not only its .py files."""

    def _swept(self, project_dir):
        with redirect_stdout(io.StringIO()):
            return {rel for _, rel in _sweep_project_files(str(project_dir))}

    def _swept_with_output(self, project_dir):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            swept = {rel for _, rel in _sweep_project_files(str(project_dir))}
        return swept, buffer.getvalue()

    def test_non_python_files_are_swept_with_their_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "notes.txt")
            _write(project / "pyproject.toml")
            _write(project / "langgraph.json")
            _write(project / "agent.py")
            _write(project / "docs" / "manual.pdf")
            _write(project / "src" / "pkg" / "prompts" / "system.md")

            swept = self._swept(project)

        self.assertEqual(
            swept,
            {
                "notes.txt",
                "pyproject.toml",
                "langgraph.json",
                "agent.py",
                os.path.join("docs", "manual.pdf"),
                os.path.join("src", "pkg", "prompts", "system.md"),
            },
        )

    def test_generated_hidden_and_host_local_paths_are_left_behind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "keep.txt")
            _write(project / ".env", "OPENAI_API_KEY=real")
            _write(project / ".config" / "settings.json")
            _write(project / "stubs" / "Old.py")
            _write(project / "grpc_stubs" / "old_pb2.py")
            _write(project / "docker_container" / "Agent" / "Dockerfile")
            _write(project / "__pycache__" / "agent.cpython-311.pyc")
            _write(project / "venv" / "lib" / "site.py")
            _write(project / "node_modules" / "left-pad" / "index.js")
            _write(project / "proj.egg-info" / "PKG-INFO")
            _write(project / "compiled.pyc")
            _write(project / "client.pem", "-----BEGIN PRIVATE KEY-----")

            swept = self._swept(project)

        self.assertEqual(swept, {"keep.txt"})

    def test_generated_directory_names_are_only_reserved_at_the_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "stubs" / "Generated.py")
            _write(project / "src" / "stubs" / "handwritten.py")

            swept = self._swept(project)

        self.assertEqual(swept, {os.path.join("src", "stubs", "handwritten.py")})

    def test_symlinks_are_not_followed_and_are_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = Path(tmpdir) / "outside"
            _write(outside / "secret.txt", "not ours")
            project = Path(tmpdir) / "project"
            _write(project / "real.txt")
            (project / "link.txt").symlink_to(project / "real.txt")
            (project / "escape").symlink_to(outside)

            swept, output = self._swept_with_output(project)

        self.assertEqual(swept, {"real.txt"})
        self.assertIn("link.txt", output)
        self.assertIn("escape", output)

    def test_project_requirements_does_not_replace_the_generated_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "requirements.txt", "yfinance==0.1\n")
            yaml_path = project / "ExampleAgent.yaml"
            yaml_path.write_text(yaml.safe_dump({"agent": {"name": "ExampleAgent"}}))
            agent_file = _write(project / "agent.py", "print('ok')\n")
            output_dir = os.path.join(tmpdir, "out")

            generate_docker(
                str(yaml_path),
                str(agent_file),
                output_dir=output_dir,
                project_dir=str(project),
            )

            requirements = _read_requirements(output_dir)

        self.assertEqual(requirements, BASE_AGENT_REQUIREMENTS)

    def test_agent_context_receives_the_swept_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            yaml_path = project / "ExampleAgent.yaml"
            yaml_path.write_text(yaml.safe_dump({"agent": {"name": "ExampleAgent"}}))
            agent_file = _write(project / "agent.py", "print('ok')\n")
            _write(project / "data" / "handbook.pdf", "%PDF-1.4")
            output_dir = os.path.join(tmpdir, "out")

            generate_docker(
                str(yaml_path),
                str(agent_file),
                output_dir=output_dir,
                project_dir=str(project),
            )

            copied = Path(output_dir) / "data" / "handbook.pdf"

            self.assertEqual(copied.read_text(), "%PDF-1.4")

    def test_workflow_context_receives_the_swept_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            workflow_file = _write(project / "workflow.py", "print('ok')\n")
            _write(project / "config" / "langgraph.json", "{}")
            output_dir = os.path.join(tmpdir, "out")

            generate_workflow_docker(
                str(workflow_file),
                [],
                output_dir=output_dir,
                project_dir=str(project),
            )

            copied = Path(output_dir) / "config" / "langgraph.json"

            self.assertEqual(copied.read_text(), "{}")

    def test_private_keys_are_recognized_by_armor_not_by_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
            _write(project / "server.pem", "-----BEGIN RSA PRIVATE KEY-----\nabc\n")
            _write(project / "keystore.p12", "binary-ish")
            _write(project / "ca.pem", "-----BEGIN CERTIFICATE-----\nabc\n")
            _write(project / "notes.key", "this is a text file about keys")

            swept, output = self._swept_with_output(project)

        self.assertEqual(swept, {"ca.pem", "notes.key"})
        self.assertIn("id_rsa", output)
        self.assertIn("keystore.p12", output)

    def test_skipped_hidden_paths_are_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "agent.py")
            _write(project / ".env", "OPENAI_API_KEY=real")
            _write(project / ".streamlit" / "config.toml")

            swept, output = self._swept_with_output(project)

        self.assertEqual(swept, {"agent.py"})
        self.assertIn(".env", output)
        self.assertIn(".streamlit", output)

    def test_an_oversized_context_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "dataset.bin", "x" * 4096)
            _write(project / "agent.py")

            original = stub_generator._LARGE_CONTEXT_BYTES
            stub_generator._LARGE_CONTEXT_BYTES = 1024
            try:
                swept, output = self._swept_with_output(project)
            finally:
                stub_generator._LARGE_CONTEXT_BYTES = original

        self.assertEqual(swept, {"dataset.bin", "agent.py"})
        self.assertIn("dataset.bin", output)

    def test_a_normal_project_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "agent.py")
            _write(project / "notes.txt")
            _write(project / "__pycache__" / "agent.cpython-311.pyc")

            _, output = self._swept_with_output(project)

        self.assertEqual(output, "")

    def test_the_build_context_is_not_swept_into_itself(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            yaml_path = project / "ExampleAgent.yaml"
            yaml_path.write_text(yaml.safe_dump({"agent": {"name": "ExampleAgent"}}))
            agent_file = _write(project / "agent.py", "print('ok')\n")
            # Not docker_container/, so nothing but exclude_dir keeps this out.
            output_dir = project / "build_context"

            generate_docker(
                str(yaml_path),
                str(agent_file),
                output_dir=str(output_dir),
                project_dir=str(project),
            )

            nested = list(output_dir.rglob("build_context"))

        self.assertEqual(nested, [])

    def test_an_empty_file_does_not_break_the_sweep(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            # An empty __init__.py is in nearly every Python project, and it
            # used to make the largest-file bookkeeping compare a path to None.
            _write(project / "src" / "__init__.py", "")
            _write(project / "src" / "agent.py", "print('ok')\n")

            swept = self._swept(project)

        self.assertEqual(
            swept,
            {os.path.join("src", "__init__.py"), os.path.join("src", "agent.py")},
        )

    def test_ordinary_repo_furniture_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "agent.py")
            _write(project / ".gitignore", "*.pyc")
            _write(project / ".git" / "config")
            _write(project / ".venv" / "pyvenv.cfg")
            _write(project / ".mypy_cache" / "cache.json")

            swept, output = self._swept_with_output(project)

        self.assertEqual(swept, {"agent.py"})
        self.assertEqual(output, "")

    def test_host_local_directories_are_reported_because_they_used_to_ship(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "agent.py")
            # These held .py files that the old .py-only sweep shipped, so
            # dropping them is a behavior change and has to be visible.
            _write(project / "venv" / "lib" / "site.py")
            _write(project / "proj.egg-info" / "PKG-INFO")
            _write(project / "__pycache__" / "agent.cpython-311.pyc")

            swept, output = self._swept_with_output(project)

        self.assertEqual(swept, {"agent.py"})
        self.assertIn("venv", output)
        self.assertIn("proj.egg-info", output)
        self.assertNotIn("__pycache__", output)


class StubDestinationTests(unittest.TestCase):
    """A stub replaces the real module at its entrypoint path, so it is written
    to exactly that one location. A stub that cannot be placed there is an error.
    """

    def test_entrypoint_mapping_is_the_only_destination(self):
        destination = _stub_destination(
            "/stubs/split_agent.py", {"split_agent.py": "agents/split_agent.py"}
        )
        self.assertEqual(destination, "agents/split_agent.py")

    def test_flat_entrypoint_stays_flat(self):
        destination = _stub_destination(
            "/stubs/split_agent.py", {"split_agent.py": "split_agent.py"}
        )
        self.assertEqual(destination, "split_agent.py")

    def test_unmapped_stub_is_an_error(self):
        with self.assertRaises(ValueError):
            _stub_destination("/stubs/split_agent.py", {})

    def test_stub_missing_from_a_populated_map_is_an_error(self):
        with self.assertRaises(ValueError):
            _stub_destination(
                "/stubs/retail_flow_agent.py",
                {"RetailAnalyticsAgent.py": "src/retail_flow_agent.py"},
            )

    def test_unsafe_entrypoint_is_an_error(self):
        with self.assertRaises(ValueError):
            _stub_destination(
                "/stubs/split_agent.py", {"split_agent.py": "../../etc/passwd"}
            )


class GenerateWorkflowDockerStubPlacementTests(unittest.TestCase):
    def test_stub_lands_both_flat_and_at_its_entrypoint_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow_file = Path(tmpdir) / "workflow.py"
            workflow_file.write_text("from agents.split_agent import SplitAgent\n")

            stub_file = Path(tmpdir) / "stubs" / "split_agent.py"
            stub_file.parent.mkdir()
            stub_file.write_text("class SplitAgent:\n    pass\n")

            output_dir = os.path.join(tmpdir, "out")
            generate_workflow_docker(
                str(workflow_file),
                [str(stub_file)],
                output_dir=output_dir,
                stub_entrypoints={"split_agent.py": "agents/split_agent.py"},
            )

            nested_path = Path(output_dir) / "agents" / "split_agent.py"
            flat_path = Path(output_dir) / "split_agent.py"
            self.assertIn("class SplitAgent", nested_path.read_text())
            self.assertIn("class SplitAgent", flat_path.read_text())


class PlatformPinTests(unittest.TestCase):
    """Each forced package resolves to the higher of our pin and the app's ask."""

    def _context(self, requirements, workflow=False):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            output_dir = os.path.join(tmpdir, "out")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                if workflow:
                    workflow_file = _write(project / "workflow.py", "print('ok')\n")
                    generate_workflow_docker(
                        str(workflow_file),
                        [],
                        output_dir=output_dir,
                        requirements=requirements,
                    )
                else:
                    yaml_path = project / "ExampleAgent.yaml"
                    yaml_path.write_text(
                        yaml.safe_dump({"agent": {"name": "ExampleAgent"}})
                    )
                    agent_file = _write(project / "agent.py", "print('ok')\n")
                    generate_docker(
                        str(yaml_path),
                        str(agent_file),
                        output_dir=output_dir,
                        requirements=requirements,
                    )
            dockerfile = _read_dockerfile(output_dir)
        written = dockerfile.split("printf '%s\\n' ")[1]
        written = written.split(" > /tmp/overrides.txt")[0]
        notes = [
            line.strip()
            for line in buffer.getvalue().splitlines()
            if line.startswith(("  Note:", "  Warning:"))
        ]
        return [entry.strip("'") for entry in written.split()], notes

    def test_every_platform_pin_is_exact(self):
        for pin in PLATFORM_PINS:
            with self.subTest(pin=pin):
                self.assertRegex(pin, r"^[a-z0-9-]+==[0-9][0-9a-z.]*$")

    def test_pins_come_from_the_base_requirements(self):
        forced = {pin.split("==")[0] for pin in PLATFORM_PINS}
        self.assertEqual(forced, set(stub_generator._FORCED_FROM_BASE))
        for pin in PLATFORM_PINS:
            with self.subTest(pin=pin):
                self.assertIn(pin, BASE_AGENT_REQUIREMENTS)

    def test_grpcio_tools_is_forced_wherever_protobuf_is(self):
        # protoc stamps its own generation into the *_pb2.py it writes.
        forced = {pin.split("==")[0] for pin in PLATFORM_PINS}
        if "protobuf" in forced:
            self.assertIn("grpcio-tools", forced)

    def test_the_pin_holds_and_stays_quiet_when_nothing_newer_is_asked(self):
        for requirements in (
            [],
            ["protobuf>=5.29.0"],
            ["protobuf==5.29.6"],
            ["streamlit==1.31.1"],
        ):
            with self.subTest(requirements=requirements):
                overrides, notes = self._context(requirements)
                self.assertEqual(overrides, list(PLATFORM_PINS))
                self.assertEqual(notes, [])

    def test_an_app_asking_for_newer_wins(self):
        overrides, notes = self._context(["protobuf>=7"])
        self.assertIn("protobuf>=7", overrides)
        self.assertNotIn("protobuf==5.29.6", overrides)
        self.assertEqual(
            notes, ["Note: 'protobuf>=7' outranks the platform pin protobuf==5.29.6"]
        )

    def test_an_app_asking_for_older_loses_and_is_told(self):
        overrides, notes = self._context(["protobuf<5"])
        self.assertIn("protobuf==5.29.6", overrides)
        self.assertEqual(
            notes, ["Warning: the platform pin protobuf==5.29.6 breaks 'protobuf<5'"]
        )

    def test_the_workflow_context_decides_the_same_way(self):
        overrides, notes = self._context(["protobuf>=7"], workflow=True)
        self.assertIn("protobuf>=7", overrides)
        self.assertEqual(len(notes), 1)

    def test_override_entries_are_quoted_for_the_shell(self):
        # An unquoted `protobuf>=7` would be a redirect, not an argument.
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            yaml_path = project / "ExampleAgent.yaml"
            yaml_path.write_text(yaml.safe_dump({"agent": {"name": "ExampleAgent"}}))
            agent_file = _write(project / "agent.py", "print('ok')\n")
            output_dir = os.path.join(tmpdir, "out")
            with redirect_stdout(io.StringIO()):
                generate_docker(
                    str(yaml_path),
                    str(agent_file),
                    output_dir=output_dir,
                    requirements=["protobuf>=7"],
                )
            dockerfile = _read_dockerfile(output_dir)

        self.assertIn("'protobuf>=7'", dockerfile)

    def test_both_dockerfiles_install_with_the_overrides_and_report(self):
        for workflow in (False, True):
            with self.subTest(workflow=workflow):
                with tempfile.TemporaryDirectory() as tmpdir:
                    project = Path(tmpdir)
                    output_dir = os.path.join(tmpdir, "out")
                    with redirect_stdout(io.StringIO()):
                        if workflow:
                            wf = _write(project / "workflow.py", "print('ok')\n")
                            generate_workflow_docker(str(wf), [], output_dir=output_dir)
                        else:
                            yaml_path = project / "ExampleAgent.yaml"
                            yaml_path.write_text(
                                yaml.safe_dump({"agent": {"name": "ExampleAgent"}})
                            )
                            agent = _write(project / "agent.py", "print('ok')\n")
                            generate_docker(
                                str(yaml_path), str(agent), output_dir=output_dir
                            )
                    dockerfile = _read_dockerfile(output_dir)

                install = dockerfile.split("RUN uv pip check")[0]
                self.assertIn("--overrides /tmp/overrides.txt", install)
                self.assertIn("uv pip check --system", dockerfile)
                for pin in PLATFORM_PINS:
                    self.assertIn(pin, dockerfile.split("NOTE:")[1])


if __name__ == "__main__":
    unittest.main()
