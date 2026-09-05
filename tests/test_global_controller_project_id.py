"""_load_config() must mint a project_id when a config file omits one, and persist it back
to the file so the same value survives a reload_config() or process restart -- not a fresh
uuid on every load.
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml

from canyonos_core.controller.global_controller import GlobalController

UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _write_config(body):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(body)
    f.close()
    return f.name


class LoadConfigProjectIdTests(unittest.TestCase):
    def test_generates_and_persists_project_id_when_missing(self):
        config_path = _write_config("agents: []\npoll_interval: 5\n")
        try:
            config = GlobalController._load_config(config_path)

            self.assertTrue(UUID_HEX_RE.match(config["project_id"]))

            with open(config_path) as f:
                on_disk = yaml.safe_load(f)
            self.assertEqual(on_disk["project_id"], config["project_id"])
        finally:
            os.unlink(config_path)

    def test_reload_reuses_the_persisted_project_id_instead_of_minting_a_new_one(self):
        config_path = _write_config("agents: []\npoll_interval: 5\n")
        try:
            first = GlobalController._load_config(config_path)
            second = GlobalController._load_config(config_path)

            self.assertEqual(first["project_id"], second["project_id"])
        finally:
            os.unlink(config_path)

    def test_existing_project_id_is_left_untouched(self):
        config_path = _write_config(
            'agents: []\nproject_id: "11111111-1111-1111-1111-111111111111"\n'
        )
        try:
            config = GlobalController._load_config(config_path)

            self.assertEqual(config["project_id"], "11111111-1111-1111-1111-111111111111")

            with open(config_path) as f:
                contents = f.read()
            # No second project_id line got appended alongside the existing one.
            self.assertEqual(contents.count("project_id"), 1)
        finally:
            os.unlink(config_path)


if __name__ == "__main__":
    unittest.main()
