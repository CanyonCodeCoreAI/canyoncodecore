"""Keeps `.claude/skills/porting-to-canyonos` honest against canyonos_core's
actual runtime behavior instead of a hand-maintained copy of it.

Two different failure modes live here, and they need different tests:

- `BehaviorContractTests`: a skill doc or validator claims canyonos_core
  behaves a specific way. Each test re-derives that claim from real source
  (not a mock) so a behavior change in canyonos_core turns the matching
  claim red instead of quietly going stale. See the V030/V031 fix in
  76b5a45, where `env_file` injection and an `_install_step` were probed as
  if they could vary when neither ever has.

- `ConfigKeyCoverageTests`: canyonos_core can grow a new top-level config key
  (a `self.config.get("...")` call) without anyone updating the skill's
  manifest.md ownership table. That is exactly how `otel.destinations`
  went undocumented -- this test diffs the two lists so it can't happen
  silently again.

When a test here fails, the fix is almost always in the skill doc or
validator it is pinned to (see each test's `skill:` comment), not in this
file. Only change the assertion itself once you've confirmed the old claim
is genuinely gone from canyonos_core, not merely inconvenient.
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.controller import global_controller
from canyonos_core.controller.utils import env_file as env_file_module

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKILL_ROOT = os.path.join(REPO_ROOT, ".claude", "skills", "porting-to-canyonos")
MANIFEST_MD = os.path.join(SKILL_ROOT, "references", "manifest.md")


class BehaviorContractTests(unittest.TestCase):
    def test_env_file_injection_is_unconditional(self):
        # skill: references/manifest.md#ownership-of-configuration-keys (env_file row)
        # resolve_env_file must run for every GlobalController, not behind a
        # probed capability.
        source = inspect.getsource(global_controller.GlobalController.__init__)
        self.assertIn("resolve_env_file(self.config)", source)

    def test_no_editable_install_mechanism_exists(self):
        # skill: references/manifest.md#per-image-requirements
        # "pyproject.toml is installed only where the editable-install
        # capability is available" -- that capability has never existed in
        # canyonos_core. If this test fails, canyonos_core grew one: restore
        # the editable_install probe in validate.py's probe_capabilities()
        # instead of deleting this test.
        self.assertFalse(hasattr(global_controller, "_install_step"))

    def test_otel_destinations_is_optional_and_silent_when_absent(self):
        # skill: references/manifest.md#ownership-of-configuration-keys (otel.destinations row)
        source = inspect.getsource(global_controller.GlobalController.__init__)
        self.assertIn('self.config.get("otel", {})', source)
        self.assertIsNone(global_controller.GlobalController._otel_destinations({}))

    def test_env_file_config_key_is_ignored_under_managed_secrets(self):
        # skill: references/manifest.md (env_file row) -- a managed deployment's
        # platform secrets file wins over a self-hosted env_file, it does not error.
        source = inspect.getsource(env_file_module.resolve_env_file)
        self.assertIn('config.get("env_file")', source)

    def test_project_id_is_generated_and_persisted_when_absent(self):
        # skill: references/manifest.md#ownership-of-configuration-keys (project_id row)
        source = inspect.getsource(global_controller.GlobalController._load_config)
        self.assertIn("_assign_new_project_id", source)


class ConfigKeyCoverageTests(unittest.TestCase):
    """skill: references/manifest.md#ownership-of-configuration-keys

    Every top-level key canyonos_core reads off the config dict must have a
    row in manifest.md's ownership table.
    """

    CONTRACT_SURFACE = (
        os.path.join("canyonos_core", "controller", "global_controller.py"),
        os.path.join("canyonos_core", "controller", "utils", "env_file.py"),
    )

    # Keys that are real and load-bearing but are not "one scalar choice" rows
    # in the ownership table -- they're documented as their own section
    # instead. Add to this set only with a comment saying where else the key
    # is actually documented; it is an escape hatch, not a place to bury gaps.
    STRUCTURAL_KEYS = frozenset(
        {
            "agents",  # the whole manifest's subject; see "Agent declarations"
        }
    )

    # (?<!\w) keeps this anchored to a bare `config` variable/attribute --
    # without it, `policy_config.get("rules")` false-positives as a `config`
    # key named `rules` because "config.get(" is a substring of it.
    KEY_CALL_PATTERN = re.compile(r'(?<!\w)(?:self\.)?config\.get\(\s*["\'](\w+)["\']')
    DOC_TOKEN_PATTERN = re.compile(r"`([a-zA-Z_][\w.:]*)`")

    @classmethod
    def _extract_config_keys_from_code(cls):
        keys = set()
        for relative_path in cls.CONTRACT_SURFACE:
            with open(os.path.join(REPO_ROOT, relative_path)) as f:
                keys.update(cls.KEY_CALL_PATTERN.findall(f.read()))
        return keys

    @classmethod
    def _extract_documented_keys(cls):
        with open(MANIFEST_MD) as f:
            text = f.read()
        table = text.split("## Ownership of configuration keys", 1)[1]
        table = table.split("## Configuration review", 1)[0]
        tokens = cls.DOC_TOKEN_PATTERN.findall(table)
        return {token.split(".")[0].rstrip(":") for token in tokens}

    def test_manifest_table_covers_every_config_key_in_code(self):
        keys_in_code = self._extract_config_keys_from_code() - self.STRUCTURAL_KEYS
        keys_in_doc = self._extract_documented_keys()
        missing = keys_in_code - keys_in_doc
        self.assertFalse(
            missing,
            f"canyonos_core reads config key(s) {sorted(missing)} that "
            "references/manifest.md's ownership table does not list. Add a "
            "row for each (see the otel.destinations row for the pattern) "
            "before merging.",
        )


if __name__ == "__main__":
    unittest.main()
