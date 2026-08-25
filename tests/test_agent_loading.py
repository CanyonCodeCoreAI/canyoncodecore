import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.stub_generator import _write_requirements


AGENT_MODULE = '''
class Greeter(object):
    """A class, the way agents have always been written."""

    def hello(self, name):
        return f"hello {name}"


class _Prebuilt(object):
    """Stands in for a CompiledStateGraph / Runnable / Crew."""

    def __init__(self, greeting):
        self.greeting = greeting

    def invoke(self, name):
        return f"{self.greeting} {name}"


# What LangGraph, LCEL and CrewAI actually export: an object, not a class.
graph = _Prebuilt("hi")
'''


def _load(agent_name, agent_path):
    """The body of LocalController._load_agent, isolated from Redis and gRPC."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_under_test", agent_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_under_test"] = module
    spec.loader.exec_module(module)

    target = getattr(module, agent_name)
    return target() if isinstance(target, type) else target


class LoadAgentTargetTests(unittest.TestCase):
    """A yaml `agent.name` may point at a class or at an already-built object."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "agent_mod.py")
        with open(self.path, "w") as f:
            f.write(AGENT_MODULE)

    def test_class_is_instantiated(self):
        agent = _load("Greeter", self.path)
        self.assertEqual(agent.hello("world"), "hello world")

    def test_instance_is_used_as_is(self):
        # Calling this one would raise TypeError, which _load_agent swallows,
        # leaving the replica alive and answering "No agent loaded".
        agent = _load("graph", self.path)
        self.assertEqual(agent.invoke("world"), "hi world")

    def test_instance_keeps_its_construction_arguments(self):
        # The reason this matters beyond convenience: `agent_class()` takes no
        # arguments, so a configured agent had nowhere to receive its config.
        agent = _load("graph", self.path)
        self.assertEqual(agent.greeting, "hi")


class WriteRequirementsTests(unittest.TestCase):
    BASE = "grpcio\nredis\npyyaml\n"

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "requirements.txt")

    def _write(self, extra):
        _write_requirements(self.path, self.BASE, extra)
        with open(self.path) as f:
            return f.read().split()

    def test_declared_requirements_are_appended(self):
        self.assertEqual(
            self._write(["langchain>=1.0.0", "langgraph>=1.0.0"]),
            ["grpcio", "redis", "pyyaml", "langchain>=1.0.0", "langgraph>=1.0.0"],
        )

    def test_none_leaves_the_runtime_list_alone(self):
        self.assertEqual(self._write(None), ["grpcio", "redis", "pyyaml"])

    def test_a_bare_string_is_accepted(self):
        self.assertEqual(
            self._write("html2text"), ["grpcio", "redis", "pyyaml", "html2text"]
        )

    def test_runtime_dependencies_cannot_be_repinned(self):
        # A second `redis` line would let a project pin a version the runtime
        # cannot actually run against.
        self.assertEqual(self._write(["redis>=5.0", "redis"]), ["grpcio", "redis", "pyyaml"])

    def test_blank_entries_are_dropped(self):
        self.assertEqual(self._write(["", "  ", "html2text"]),
                         ["grpcio", "redis", "pyyaml", "html2text"])

    def test_extras_marker_is_matched_against_the_base_name(self):
        self.assertEqual(self._write(["redis[hiredis]"]), ["grpcio", "redis", "pyyaml"])


if __name__ == "__main__":
    unittest.main()
