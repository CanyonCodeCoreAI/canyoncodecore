import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox" / "localstack_ssm" / "project" / "agents"))

from reverse_agent import ReverseAgent


class ReverseAgentTests(unittest.TestCase):
    def test_reverse_agent_reverses_ascii_text(self):
        self.assertEqual(ReverseAgent().reverse("ventis"), "sitnev")

    def test_reverse_agent_keeps_empty_string_empty(self):
        self.assertEqual(ReverseAgent().reverse(""), "")


if __name__ == "__main__":
    unittest.main()
