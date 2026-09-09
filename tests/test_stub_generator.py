import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.stub_generator import (
    BASE_AGENT_REQUIREMENTS,
    BASE_WORKFLOW_REQUIREMENTS,
    _stub_destination,
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


class StubDestinationTests(unittest.TestCase):
    """A stub replaces the real module at its entrypoint path, so it is written
    to exactly that one location. Flat is only a fallback for a stub with no
    entrypoint mapping, or one whose mapping escapes the build context.
    """

    def test_unmapped_stub_falls_back_to_flat(self):
        self.assertEqual(_stub_destination("/stubs/split_agent.py", {}), "split_agent.py")

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

    def test_unsafe_entrypoint_falls_back_to_flat(self):
        destination = _stub_destination(
            "/stubs/split_agent.py", {"split_agent.py": "../../etc/passwd"}
        )
        self.assertEqual(destination, "split_agent.py")


class GenerateWorkflowDockerStubPlacementTests(unittest.TestCase):
    def test_stub_lands_only_at_its_entrypoint_path(self):
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
            self.assertFalse(flat_path.exists(), "stub must not be duplicated flat")


if __name__ == "__main__":
    unittest.main()
