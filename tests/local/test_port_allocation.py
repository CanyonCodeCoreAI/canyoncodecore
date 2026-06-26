import unittest

from tests.support.runtime_fakes import FakeController
from tests.support.runtime_fakes import make_instance
from ventis.controller.runtime_manager import allocate_host_port
from ventis.controller.runtime_manager import resolve_local_replica_placements as resolve_replica_placements
from ventis.controller.runtime_manager import RuntimeManager


class LocalPlacementTests(unittest.TestCase):
    def test_resolve_replica_placements_uses_dynamic_ports(self):
        placements = resolve_replica_placements({"name": "Alpha", "replicas": 2})

        self.assertEqual(
            placements,
            [
                {"host": "localhost", "host_port": None},
                {"host": "localhost", "host_port": None},
            ],
        )

    def test_resolve_replica_placements_defaults_to_one_replica(self):
        placements = resolve_replica_placements({"name": "Alpha"})

        self.assertEqual(placements, [{"host": "localhost", "host_port": None}])

    def test_resolve_replica_placements_accepts_numeric_string_replicas(self):
        placements = resolve_replica_placements({"name": "Alpha", "replicas": "3"})

        self.assertEqual(len(placements), 3)
        self.assertTrue(all(item["host_port"] is None for item in placements))

    def test_resolve_replica_placements_allows_zero_replicas(self):
        placements = resolve_replica_placements({"name": "Alpha", "replicas": 0})

        self.assertEqual(placements, [])

    def test_resolve_replica_placements_rejects_legacy_static_config(self):
        legacy_specs = [
            {"name": "Legacy", "host": "localhost", "replicas": 1},
            {"name": "Legacy", "port": 9000, "replicas": 1},
            {"name": "Legacy", "replicas": [{"host": "localhost", "port": 9000}]},
        ]

        for spec in legacy_specs:
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ValueError, "Legacy YAML host/port"):
                    resolve_replica_placements(spec)


class LocalPortAllocationTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.manager = RuntimeManager(self.controller, self.controller.redis)

    def write_instance(self, instance):
        key = self.manager._instance_key(
            instance["provider"],
            instance["agent_name"],
            int(instance["replica_index"]),
        )
        self.controller.redis.hset_multiple(key, instance)

    def test_allocate_host_port_uses_first_available_port(self):
        self.write_instance(make_instance("Alpha", 0, host_port=8000))
        self.write_instance(make_instance("Beta", 0, host_port=8002))

        port = allocate_host_port(self.manager, "localhost")

        self.assertEqual(port, 8001)

    def test_allocate_host_port_ignores_different_hosts(self):
        self.write_instance(make_instance("Remote", 0, host="10.0.0.5", host_port=8000))

        port = allocate_host_port(self.manager, "localhost")

        self.assertEqual(port, 8000)

    def test_allocate_host_port_skips_contiguous_used_ports(self):
        for index, port in enumerate((8000, 8001, 8002, 8003)):
            self.write_instance(make_instance(f"Agent{index}", 0, host_port=port))

        port = allocate_host_port(self.manager, "localhost")

        self.assertEqual(port, 8004)

    def test_allocate_host_port_tracks_ports_per_host(self):
        self.write_instance(make_instance("Local", 0, host="localhost", host_port=8000))
        self.write_instance(make_instance("Remote", 0, host="10.0.0.5", host_port=8000))

        local_port = allocate_host_port(self.manager, "localhost")
        remote_port = allocate_host_port(self.manager, "10.0.0.5")

        self.assertEqual(local_port, 8001)
        self.assertEqual(remote_port, 8001)

    def test_allocate_host_port_handles_string_ports_in_records(self):
        instance = make_instance("Alpha", 0, host_port=8000)
        instance["host_port"] = "8000"
        self.write_instance(instance)

        port = allocate_host_port(self.manager, "localhost")

        self.assertEqual(port, 8001)

    def test_allocate_host_port_ignores_replaced_instance(self):
        existing = make_instance("Alpha", 0, host_port=8000)
        self.write_instance(existing)

        port = allocate_host_port(
            self.manager,
            "localhost",
            ignore_instance_id="local:Alpha:0",
        )

        self.assertEqual(port, 8000)

    def test_requested_host_port_is_respected(self):
        self.write_instance(make_instance("Alpha", 0, host_port=8000))

        port = allocate_host_port(self.manager, "localhost", requested_host_port=9100)

        self.assertEqual(port, 9100)


if __name__ == "__main__":
    unittest.main()
