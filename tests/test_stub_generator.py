import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis import stub_generator
from ventis.stub_generator import (
    BASE_AGENT_REQUIREMENTS,
    BASE_WORKFLOW_REQUIREMENTS,
    _sweep_project_files,
    generate_docker,
    generate_workflow_docker,
)


def _read_requirements(output_dir):
    return (Path(output_dir) / "requirements.txt").read_text().splitlines()


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

    def test_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            _write(project / "real.txt")
            (project / "link.txt").symlink_to(project / "real.txt")

            swept = self._swept(project)

        self.assertEqual(swept, {"real.txt"})

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


if __name__ == "__main__":
    unittest.main()
