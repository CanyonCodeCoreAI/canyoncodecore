"""Bug A: reload_config() must re-sync the telemetry writer's cached project_id.

__init__ calls assign_project_id() once, at boot. reload_config() -- the method meant to run
whenever the box's config changes -- rebuilt self.config/self.controllers/self.poll_interval and
republished the routing snapshot, but never called assign_project_id() again, so a box reclaimed
by a new project kept tagging every telemetry write with whichever project was live at process
start, forever.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml

import canyonos_core.controller.utils.telemetry_logging as sqlmod
from canyonos_core.controller.global_controller import GlobalController


class _FakeInstanceManager:
    def publish_routing_snapshot(self, controllers):
        pass


class _FakeRedis:
    def hset_multiple(self, name, mapping):
        pass


def _write_config(project_id):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump({"agents": [], "poll_interval": 5, "project_id": project_id}, f)
    f.close()
    return f.name


def _bare_controller(config_path):
    """Build a GlobalController without running its heavy __init__, wired only with what
    reload_config() actually touches -- now including node_redis/redis, since
    reload_config() also calls _write_identity() (Bug E)."""
    controller = GlobalController.__new__(GlobalController)
    controller.config_path = config_path
    controller.instance_manager = _FakeInstanceManager()
    controller.node_redis = {}
    controller.redis = _FakeRedis()
    return controller


class ReloadConfigResyncTests(unittest.TestCase):
    def setUp(self):
        sqlmod._project_id = None

    def tearDown(self):
        sqlmod._project_id = None

    def test_reload_config_resyncs_project_id_to_the_new_value(self):
        config_path = _write_config("22222222-2222-2222-2222-222222222222")
        try:
            controller = _bare_controller(config_path)
            sqlmod.assign_project_id("11111111-1111-1111-1111-111111111111")

            controller.reload_config()

            self.assertEqual(sqlmod._project_id, "22222222-2222-2222-2222-222222222222")
        finally:
            os.unlink(config_path)

    def test_reload_config_resyncs_on_every_switch_not_just_the_first(self):
        """A box reclaimed A -> B -> A must resync every time, not just once."""
        config_a = _write_config("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        config_b = _write_config("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        try:
            controller = _bare_controller(config_a)

            controller.reload_config()
            self.assertEqual(sqlmod._project_id, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

            controller.config_path = config_b
            controller.reload_config()
            self.assertEqual(sqlmod._project_id, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

            controller.config_path = config_a
            controller.reload_config()
            self.assertEqual(sqlmod._project_id, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        finally:
            os.unlink(config_a)
            os.unlink(config_b)

if __name__ == "__main__":
    unittest.main()
