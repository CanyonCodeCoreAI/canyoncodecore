import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.reconciler import state
from canyonos_core.reconciler.reconciler import Reconciler


class _FakeRedis:
    """Stands in for RedisClient, decoding to str the way the real client does."""

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}
        self.lists = {}

    def set(self, key, value):
        self.strings[key] = str(value)

    def get(self, key):
        value = self.strings.get(key)
        return None if value is None else str(value)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            self.sets.pop(key, None)
            self.lists.pop(key, None)

    def incrby(self, key, amount=1):
        new_value = int(self.strings.get(key, 0)) + int(amount)
        self.strings[key] = str(new_value)
        return new_value

    def lpush(self, key, *values):
        self.lists.setdefault(key, [])[:0] = [str(v) for v in values]

    def rpop(self, key):
        values = self.lists.get(key)
        if not values:
            return None
        return values.pop()

    def brpop(self, key, timeout=0):
        """Non-blocking in the fake: the tail value or None, timeout ignored."""
        return self.rpop(key)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = str(value)

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update({k: str(v) for k, v in mapping.items()})

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hdel(self, name, field):
        self.hashes.setdefault(name, {}).pop(field, None)

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)

    def srem(self, name, *values):
        self.sets.setdefault(name, set()).difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(name, set()))

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        return [key for key in sorted(keys) if key.startswith(prefix)]


class _RaisingRedis(_FakeRedis):
    def hgetall(self, name):
        raise RuntimeError("connection refused")


class _FakeInstanceManager:
    """Records what the reconciler asked for without touching Docker."""

    def __init__(self, instances=None, raise_for=None):
        self.instances = instances or {}
        self.raise_for = raise_for
        self.removed = []
        self.ensure_calls = []

    def list_instances(self, agent_name=None):
        if self.raise_for and agent_name == self.raise_for:
            raise RuntimeError("boom")
        return list(self.instances.get(agent_name, []))

    def remove_instance(self, instance_id):
        self.removed.append(instance_id)

    def ensure_instances(self, agent_specs):
        self.ensure_calls.append(agent_specs)
        return []

    @staticmethod
    def _instance_id_from_record(instance):
        return (
            f"{instance['provider']}:{instance['agent_name']}:"
            f"{int(instance['replica_index'])}"
        )


ALPHA_SPEC = {"name": "Alpha", "provider": "local", "replicas": 2}
BETA_SPEC = {"name": "Beta", "provider": "local", "replicas": 1}


def _fake_context(redis=None, agents=None, node_redis=None):
    redis = redis if redis is not None else _FakeRedis()
    agents = agents if agents is not None else [ALPHA_SPEC, BETA_SPEC]
    return SimpleNamespace(
        redis=redis,
        controllers=agents,
        agent_specs={spec["name"]: spec for spec in agents},
        config_path="/tmp/ventis.yaml",
        node_redis_for_instance=lambda instance: node_redis or redis,
        _agent_host_key=lambda host: host,
    )


def _instance(agent_name, replica_index, created_at=None, host_port=None):
    return {
        "agent_name": agent_name,
        "provider": "local",
        "replica_index": str(replica_index),
        "host": "localhost",
        "host_port": str(host_port or 8000 + replica_index),
        "created_at": str(created_at if created_at is not None else time.time()),
    }


def _bare_reconciler(context, instance_manager, **overrides):
    """Build a Reconciler without running its __init__ (no config, no Docker, no Redis)."""
    reconciler = Reconciler.__new__(Reconciler)
    reconciler.context = context
    reconciler.instance_manager = instance_manager
    reconciler.sweep_interval = 5
    reconciler.stale_after = 15
    reconciler.startup_grace = 30
    reconciler._seen_healthy = set()
    for key, value in overrides.items():
        setattr(reconciler, key, value)
    return reconciler


class DesiredStateTests(unittest.TestCase):
    def test_get_desired_returns_the_default_when_the_key_is_absent(self):
        redis = _FakeRedis()
        self.assertEqual(state.get_desired(redis, "Alpha", default=3), 3)

    def test_get_desired_parses_a_stored_int(self):
        redis = _FakeRedis()
        redis.set(state.desired_key("Alpha"), 4)
        self.assertEqual(state.get_desired(redis, "Alpha", default=1), 4)

    def test_get_desired_clamps_a_negative_stored_value_to_zero(self):
        redis = _FakeRedis()
        redis.set(state.desired_key("Alpha"), -2)
        self.assertEqual(state.get_desired(redis, "Alpha", default=1), 0)

    def test_get_desired_falls_back_to_the_default_on_a_non_integer_value(self):
        redis = _FakeRedis()
        redis.set(state.desired_key("Alpha"), "many")
        self.assertEqual(state.get_desired(redis, "Alpha", default=2), 2)

    def test_seed_desired_writes_the_configured_count_when_absent(self):
        redis = _FakeRedis()
        state.seed_desired(redis, [ALPHA_SPEC, BETA_SPEC])
        self.assertEqual(redis.get(state.desired_key("Alpha")), "2")
        self.assertEqual(redis.get(state.desired_key("Beta")), "1")

    def test_seed_desired_leaves_an_existing_value_untouched(self):
        """Redis stays authoritative so a runtime scale survives a controller restart."""
        redis = _FakeRedis()
        redis.set(state.desired_key("Alpha"), 7)
        state.seed_desired(redis, [ALPHA_SPEC])
        self.assertEqual(redis.get(state.desired_key("Alpha")), "7")

    def test_seed_desired_skips_an_agent_whose_replicas_is_a_list(self):
        redis = _FakeRedis()
        state.seed_desired(
            redis, [{"name": "Placed", "replicas": [{"host": "10.0.0.1"}]}]
        )
        self.assertIsNone(redis.get(state.desired_key("Placed")))

    def test_scale_moves_the_count_and_returns_the_new_value(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 2)
        self.assertEqual(state.scale(redis, "Alpha", 3), 5)
        self.assertEqual(redis.get(state.desired_key("Alpha")), "5")

    def test_scale_below_zero_clamps_to_zero(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        self.assertEqual(state.scale(redis, "Alpha", -4), 0)
        self.assertEqual(redis.get(state.desired_key("Alpha")), "0")

    def test_desired_agent_specs_returns_the_full_list_with_desired_counts(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 4)
        specs = state.desired_agent_specs(redis, [ALPHA_SPEC, BETA_SPEC])
        self.assertEqual(
            specs,
            [
                {"name": "Alpha", "provider": "local", "replicas": 4},
                {"name": "Beta", "provider": "local", "replicas": 1},
            ],
        )

    def test_desired_agent_specs_passes_through_non_integer_replicas_untouched(self):
        redis = _FakeRedis()
        placed = {"name": "Placed", "replicas": [{"host": "10.0.0.1"}]}
        specs = state.desired_agent_specs(redis, [ALPHA_SPEC, placed])

        # Dropping it would delete the service from the routing snapshot; leaving it
        # in fails where a list-form replicas has always failed.
        self.assertEqual([spec["name"] for spec in specs], ["Alpha", "Placed"])
        self.assertEqual(specs[1]["replicas"], [{"host": "10.0.0.1"}])


class WakeQueueTests(unittest.TestCase):
    def test_drain_returns_an_empty_set_when_nothing_is_queued(self):
        self.assertEqual(state.drain(_FakeRedis(), timeout=0), set())

    def test_drain_coalesces_a_burst_of_duplicate_signals(self):
        redis = _FakeRedis()
        for _ in range(5):
            state.request_reconcile(redis, "Alpha")

        self.assertEqual(state.drain(redis, timeout=0), {"Alpha"})
        self.assertEqual(redis.lists[state.WAKE_QUEUE_KEY], [])

    def test_request_reconcile_defaults_to_the_wake_all_signal(self):
        redis = _FakeRedis()
        state.request_reconcile(redis)
        self.assertEqual(state.drain(redis, timeout=0), {state.WAKE_ALL})


class ReapRequestTests(unittest.TestCase):
    def test_only_ids_belonging_to_the_named_agent_are_claimed(self):
        redis = _FakeRedis()
        state.request_replace(redis, "local:Alpha:1")
        state.request_replace(redis, "local:Beta:0")

        self.assertEqual(state.take_reap_requests(redis, "Alpha"), {"local:Alpha:1"})
        self.assertEqual(redis.smembers(state.REAP_SET_KEY), {"local:Beta:0"})

    def test_a_second_call_claims_nothing(self):
        redis = _FakeRedis()
        state.request_replace(redis, "local:Alpha:1")

        state.take_reap_requests(redis, "Alpha")
        self.assertEqual(state.take_reap_requests(redis, "Alpha"), set())


class ReconcileTests(unittest.TestCase):
    def _healthy(self, reconciler):
        """Force every health probe to pass without opening a socket."""
        self.enterContext(
            patch.object(Reconciler, "_accepts_connections", return_value=True)
        )
        self.enterContext(
            patch.object(Reconciler, "_reports_are_fresh", return_value=True)
        )
        return reconciler

    def test_surplus_instances_are_reaped_and_the_kept_one_is_left_alone(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        manager = _FakeInstanceManager(
            {"Alpha": [_instance("Alpha", i) for i in range(3)]}
        )
        reconciler = self._healthy(_bare_reconciler(_fake_context(redis), manager))

        reconciler.reconcile("Alpha")

        self.assertEqual(manager.removed, ["local:Alpha:1", "local:Alpha:2"])

    def test_fill_passes_specs_for_every_configured_agent(self):
        """ensure_instances republishes routing from the specs it gets, so a
        single-spec call would drop every other agent from the routing table."""
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 2)
        state.set_desired(redis, "Beta", 1)
        manager = _FakeInstanceManager()
        reconciler = self._healthy(_bare_reconciler(_fake_context(redis), manager))

        reconciler.reconcile("Alpha")

        self.assertEqual(len(manager.ensure_calls), 1)
        self.assertEqual(
            manager.ensure_calls[0],
            [
                {"name": "Alpha", "provider": "local", "replicas": 2},
                {"name": "Beta", "provider": "local", "replicas": 1},
            ],
        )

    def test_an_instance_failing_the_health_probe_is_removed(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        manager = _FakeInstanceManager({"Alpha": [_instance("Alpha", 0, created_at=0)]})
        reconciler = _bare_reconciler(_fake_context(redis), manager)

        with (
            patch.object(Reconciler, "_accepts_connections", return_value=False),
            patch.object(Reconciler, "_reports_are_fresh", return_value=True),
        ):
            reconciler.reconcile("Alpha")

        self.assertEqual(manager.removed, ["local:Alpha:0"])

    def test_a_freshly_created_instance_is_kept_during_the_startup_grace(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        manager = _FakeInstanceManager(
            {"Alpha": [_instance("Alpha", 0, created_at=time.time())]}
        )
        reconciler = _bare_reconciler(_fake_context(redis), manager)

        with (
            patch.object(Reconciler, "_accepts_connections", return_value=False),
            patch.object(Reconciler, "_reports_are_fresh", return_value=False),
        ):
            reconciler.reconcile("Alpha")

        self.assertEqual(manager.removed, [])

    def test_the_same_instance_is_removed_once_the_startup_grace_has_passed(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        manager = _FakeInstanceManager(
            {"Alpha": [_instance("Alpha", 0, created_at=time.time() - 3600)]}
        )
        reconciler = _bare_reconciler(_fake_context(redis), manager)

        with (
            patch.object(Reconciler, "_accepts_connections", return_value=False),
            patch.object(Reconciler, "_reports_are_fresh", return_value=False),
        ):
            reconciler.reconcile("Alpha")

        self.assertEqual(manager.removed, ["local:Alpha:0"])

    def test_an_instance_already_seen_healthy_gets_no_startup_grace(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        manager = _FakeInstanceManager(
            {"Alpha": [_instance("Alpha", 0, created_at=time.time())]}
        )
        reconciler = _bare_reconciler(
            _fake_context(redis), manager, _seen_healthy={"local:Alpha:0"}
        )

        with (
            patch.object(Reconciler, "_accepts_connections", return_value=False),
            patch.object(Reconciler, "_reports_are_fresh", return_value=False),
        ):
            reconciler.reconcile("Alpha")

        self.assertEqual(manager.removed, ["local:Alpha:0"])
        self.assertNotIn("local:Alpha:0", reconciler._seen_healthy)

    def test_an_agent_with_non_integer_replicas_is_skipped_not_reaped(self):
        redis = _FakeRedis()
        manager = _FakeInstanceManager()
        context = _fake_context(redis)
        context.agent_specs = {
            "Placed": {"name": "Placed", "replicas": [{"host": "h"}]}
        }
        reconciler = _bare_reconciler(context, manager)

        with self.assertLogs("canyonos_core.reconciler.reconciler", "WARNING"):
            reconciler.reconcile("Placed")

        self.assertEqual(manager.removed, [])
        self.assertEqual(manager.ensure_calls, [])

    def test_reconcile_of_an_unknown_agent_is_a_noop(self):
        manager = _FakeInstanceManager()
        reconciler = _bare_reconciler(_fake_context(), manager)

        with self.assertLogs("canyonos_core.reconciler.reconciler", "WARNING"):
            reconciler.reconcile("Nope")

        self.assertEqual(manager.removed, [])
        self.assertEqual(manager.ensure_calls, [])

    def test_reconcile_all_provisions_once_for_every_agent(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 1)
        state.set_desired(redis, "Beta", 1)
        manager = _FakeInstanceManager()
        reconciler = self._healthy(_bare_reconciler(_fake_context(redis), manager))

        reconciler.reconcile_all()

        # One ensure_instances per sweep, not one per agent: each call republishes
        # the routing snapshot to every node.
        self.assertEqual(len(manager.ensure_calls), 1)
        self.assertEqual(
            {spec["name"] for spec in manager.ensure_calls[0]}, {"Alpha", "Beta"}
        )

    def test_reconcile_all_keeps_going_when_one_agent_raises(self):
        redis = _FakeRedis()
        state.set_desired(redis, "Alpha", 0)
        state.set_desired(redis, "Beta", 0)
        manager = _FakeInstanceManager(raise_for="Alpha")
        reconciler = self._healthy(_bare_reconciler(_fake_context(redis), manager))

        with self.assertLogs("canyonos_core.reconciler.reconciler", "WARNING"):
            reconciler.reconcile_all()

        self.assertEqual(len(manager.ensure_calls), 1)


class ReportFreshnessTests(unittest.TestCase):
    def _reconciler(self, node_redis):
        context = _fake_context(node_redis=node_redis)
        return _bare_reconciler(context, _FakeInstanceManager())

    def _metrics_key(self, instance):
        return f"controller:{instance['host']}:{instance['host_port']}:metrics"

    def test_a_recent_updated_at_is_fresh(self):
        instance = _instance("Alpha", 0)
        node_redis = _FakeRedis()
        node_redis.hset(self._metrics_key(instance), "updated_at", time.time())

        self.assertTrue(self._reconciler(node_redis)._reports_are_fresh(instance))

    def test_an_updated_at_older_than_stale_after_is_not_fresh(self):
        instance = _instance("Alpha", 0)
        node_redis = _FakeRedis()
        node_redis.hset(self._metrics_key(instance), "updated_at", time.time() - 3600)

        self.assertFalse(self._reconciler(node_redis)._reports_are_fresh(instance))

    def test_a_missing_metrics_hash_is_not_fresh(self):
        self.assertFalse(
            self._reconciler(_FakeRedis())._reports_are_fresh(_instance("Alpha", 0))
        )

    def test_a_redis_blip_is_not_read_as_an_unhealthy_instance(self):
        reconciler = self._reconciler(_RaisingRedis())

        with self.assertLogs("canyonos_core.reconciler.reconciler", "WARNING"):
            self.assertTrue(reconciler._reports_are_fresh(_instance("Alpha", 0)))


if __name__ == "__main__":
    unittest.main()
