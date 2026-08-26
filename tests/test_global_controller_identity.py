"""Bug E: a Workflow container's VENTIS_PROJECT_ID/VENTIS_DATABASE_URL env vars are frozen at
launch. _write_identity() publishes the controller's current project/database identity to
every node's Redis (mirroring the existing policy:rules/routing_table:* pattern) so deploy.py's
_current_identity() can read it live instead of trusting a boot-time env var. reload_config()
must call this too, or the fix is only half-applied -- see the sibling test in
test_global_controller_reload.py for the assign_project_id() half of the same bug class.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.global_controller import GlobalController


class _FakeRedis:
    def __init__(self):
        self.hashes = {}

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))


def _bare_controller(config, node_redis=None):
    controller = GlobalController.__new__(GlobalController)
    controller.config = config
    controller.redis = _FakeRedis()
    controller.node_redis = node_redis if node_redis is not None else {}
    return controller


class WriteIdentityTests(unittest.TestCase):
    def test_publishes_project_id_and_database_url_to_self_redis_when_no_node_redis(self):
        controller = _bare_controller(
            {
                "project_id": "11111111-1111-1111-1111-111111111111",
                "database": {"url": "postgresql://example/db"},
            }
        )

        controller._write_identity()

        self.assertEqual(
            controller.redis.hgetall(GlobalController.IDENTITY_KEY),
            {
                "project_id": "11111111-1111-1111-1111-111111111111",
                "database_url": "postgresql://example/db",
            },
        )

    def test_publishes_to_every_node_not_just_self_redis(self):
        """The same lesson as Bug D: a Workflow replica on a remote EC2 host has its own
        Redis, distinct from the controller's local one. Publishing to self.redis alone
        would leave that replica reading nothing."""
        node_a = _FakeRedis()
        node_b = _FakeRedis()
        controller = _bare_controller(
            {
                "project_id": "11111111-1111-1111-1111-111111111111",
                "database": {"url": "postgresql://example/db"},
            },
            node_redis={"localhost": node_a, "172.31.0.5": node_b},
        )

        controller._write_identity()

        for node in (node_a, node_b):
            self.assertEqual(
                node.hgetall(GlobalController.IDENTITY_KEY)["project_id"],
                "11111111-1111-1111-1111-111111111111",
            )

    def test_a_second_call_with_a_new_config_overwrites_the_published_value(self):
        """Simulates what reload_config() does on a real SIGHUP: the same key must reflect
        the *new* project after a switch, not just get set once at boot."""
        node = _FakeRedis()
        controller = _bare_controller(
            {
                "project_id": "11111111-1111-1111-1111-111111111111",
                "database": {"url": "postgresql://example/old-db"},
            },
            node_redis={"localhost": node},
        )
        controller._write_identity()

        controller.config = {
            "project_id": "22222222-2222-2222-2222-222222222222",
            "database": {"url": "postgresql://example/new-db"},
        }
        controller._write_identity()

        self.assertEqual(
            node.hgetall(GlobalController.IDENTITY_KEY),
            {
                "project_id": "22222222-2222-2222-2222-222222222222",
                "database_url": "postgresql://example/new-db",
            },
        )

    def test_missing_project_id_or_database_publishes_safe_defaults(self):
        controller = _bare_controller({})

        controller._write_identity()

        self.assertEqual(
            controller.redis.hgetall(GlobalController.IDENTITY_KEY),
            {"project_id": "0", "database_url": ""},
        )


class ReloadConfigWritesIdentityTests(unittest.TestCase):
    """reload_config() itself must call _write_identity() -- not just have the method exist."""

    def test_reload_config_republishes_identity(self):
        node = _FakeRedis()
        controller = _bare_controller(
            {"project_id": "11111111-1111-1111-1111-111111111111", "agents": []},
            node_redis={"localhost": node},
        )
        controller.config_path = None
        controller.instance_manager = SimpleNamespace(publish_routing_snapshot=lambda *_: None)
        controller._load_config = lambda path: {
            "project_id": "22222222-2222-2222-2222-222222222222",
            "agents": [],
        }

        controller.reload_config()

        self.assertEqual(
            node.hgetall(GlobalController.IDENTITY_KEY)["project_id"],
            "22222222-2222-2222-2222-222222222222",
        )


if __name__ == "__main__":
    unittest.main()
