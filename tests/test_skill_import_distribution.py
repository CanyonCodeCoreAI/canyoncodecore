import os
import sys
import unittest

SKILL = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", ".claude", "skills", "porting-to-canyonos"
    )
)
sys.path.insert(0, SKILL)

from validation.dependencies import _candidate_distributions  # noqa: E402


class OpenAIAgentsImportMappingTests(unittest.TestCase):
    """`openai-agents` and its top-level import `agents` share no substring,
    so the derived-from-the-dotted-name fallback can never find it -- a
    correctly declared `openai-agents==0.17.0` requirement was reported as a
    missing distribution (W006) for every source that does `import agents`."""

    def test_openai_agents_satisfies_the_agents_import(self):
        self.assertIn("openai-agents", _candidate_distributions("agents"))

    def test_a_submodule_import_is_still_covered(self):
        self.assertIn("openai-agents", _candidate_distributions("agents.tool"))


if __name__ == "__main__":
    unittest.main()
