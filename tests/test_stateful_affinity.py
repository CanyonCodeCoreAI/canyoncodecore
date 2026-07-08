"""
Tests for stateful agent session affinity.

These tests verify that:
1. The routing table stores multiple endpoints per agent (JSON list).
2. Stateful agents get affinity bindings (same instance per request_id).
3. Stateless agents get random routing (no affinity keys written).
4. Affinity keys are cleaned up when a request completes.

Requirements:
    - A running Redis instance on localhost:6379.
    - The ventis package installed (pip install -e .).

Run:
    python -m pytest tests/test_stateful_affinity.py -v
"""

import json
import os
import socket
import sys
import uuid

import pytest
from redis.exceptions import RedisError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.instance_manager import InstanceManager
from ventis.utils.redis_client import RedisClient


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

REDIS_HOST = os.environ.get("VENTIS_TEST_REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("VENTIS_TEST_REDIS_PORT", 6379))

ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
ROUTING_STATEFUL_KEY = "routing_table:stateful"


@pytest.fixture
def redis():
    """Provide a RedisClient connected to a test Redis, flushed before each test."""
    try:
        with socket.create_connection((REDIS_HOST, REDIS_PORT), timeout=0.2):
            pass
    except OSError as exc:
        pytest.skip(f"Redis is not available at {REDIS_HOST}:{REDIS_PORT}: {exc}")

    client = RedisClient(host=REDIS_HOST, port=REDIS_PORT)
    try:
        client.client.ping()
    except RedisError as exc:
        pytest.skip(f"Redis is not available at {REDIS_HOST}:{REDIS_PORT}: {exc}")

    # Flush only keys in the test namespace to avoid clobbering production data
    for key in client.scan_keys("routing_table:*"):
        client.delete(key)
    for key in client.scan_keys("affinity:*"):
        client.delete(key)
    for key in client.scan_keys("request:*"):
        client.delete(key)
    for key in client.scan_keys("future:*"):
        client.delete(key)
    yield client


def _seed_routing_table(redis, agents):
    """Write a routing table to Redis.

    Args:
        redis: RedisClient instance.
        agents: list of dicts with keys: name, endpoints, stateful (optional).
    """
    endpoints_table = {}
    stateful_table = {}
    for agent in agents:
        endpoints_table[agent["name"]] = json.dumps(agent["endpoints"])
        if agent.get("stateful"):
            stateful_table[agent["name"]] = "true"
    redis.hset_multiple(ROUTING_ENDPOINTS_KEY, endpoints_table)
    if stateful_table:
        redis.hset_multiple(ROUTING_STATEFUL_KEY, stateful_table)


# ------------------------------------------------------------------ #
#  Tests — Routing table format                                        #
# ------------------------------------------------------------------ #

class TestRoutingTable:
    """Verify the routing table stores all replica endpoints."""

    def test_single_replica_stored_as_list(self, redis):
        """An agent with 1 replica should have a 1-element JSON list."""
        _seed_routing_table(redis, [
            {"name": "AgentA", "endpoints": ["host:5001"]},
        ])
        raw = redis.hget(ROUTING_ENDPOINTS_KEY, "AgentA")
        endpoints = json.loads(raw)
        assert endpoints == ["host:5001"]

    def test_multiple_replicas_stored_as_list(self, redis):
        """An agent with 3 replicas should have a 3-element JSON list."""
        _seed_routing_table(redis, [
            {"name": "AgentA", "endpoints": ["host:5001", "host:5002", "host:5003"]},
        ])
        raw = redis.hget(ROUTING_ENDPOINTS_KEY, "AgentA")
        endpoints = json.loads(raw)
        assert len(endpoints) == 3
        assert "host:5001" in endpoints
        assert "host:5002" in endpoints
        assert "host:5003" in endpoints

    def test_stateful_flag_written(self, redis):
        """Stateful agents should have their flag set in routing_table:stateful."""
        _seed_routing_table(redis, [
            {"name": "StatefulAgent", "endpoints": ["host:5001"], "stateful": True},
            {"name": "StatelessAgent", "endpoints": ["host:6001"]},
        ])
        assert redis.hget(ROUTING_STATEFUL_KEY, "StatefulAgent") == "true"
        assert redis.hget(ROUTING_STATEFUL_KEY, "StatelessAgent") is None

    def test_empty_endpoint_list_is_stored_as_empty_json_list(self, redis):
        """An agent with no endpoints should still store valid JSON."""
        _seed_routing_table(redis, [
            {"name": "EmptyAgent", "endpoints": []},
        ])

        assert json.loads(redis.hget(ROUTING_ENDPOINTS_KEY, "EmptyAgent")) == []


# ------------------------------------------------------------------ #
#  Tests — Affinity resolution                                         #
# ------------------------------------------------------------------ #

class TestAffinityResolution:
    """Verify the _resolve_endpoint logic for stateful vs stateless agents."""

    def _resolve_endpoint(self, redis, service, request_id):
        """Pure-function equivalent of LocalController._resolve_endpoint.

        Extracted here so we can test without spinning up the full LC.
        """
        import random as _random

        endpoints_json = redis.hget(ROUTING_ENDPOINTS_KEY, service)
        if not endpoints_json:
            return None
        endpoints = json.loads(endpoints_json)
        if not endpoints:
            return None

        is_stateful = redis.hget(ROUTING_STATEFUL_KEY, service) == "true"

        if is_stateful and request_id:
            affinity_key = f"affinity:{request_id}"
            existing = redis.hget(affinity_key, service)
            if existing:
                return existing
            chosen = _random.choice(endpoints)
            redis.hset(affinity_key, service, chosen)
            return chosen
        else:
            return _random.choice(endpoints)

    def test_stateful_affinity_is_sticky(self, redis):
        """Multiple calls with the same request_id should return the same endpoint."""
        _seed_routing_table(redis, [
            {"name": "FA", "endpoints": ["h:5001", "h:5002", "h:5003"], "stateful": True},
        ])
        rid = uuid.uuid4().hex

        first = self._resolve_endpoint(redis, "FA", rid)
        assert first in ["h:5001", "h:5002", "h:5003"]

        # Subsequent calls must return the same endpoint
        for _ in range(10):
            assert self._resolve_endpoint(redis, "FA", rid) == first

    def test_stateful_different_requests_can_differ(self, redis):
        """Different request_ids can (but don't have to) map to different instances."""
        _seed_routing_table(redis, [
            {"name": "FA", "endpoints": ["h:5001", "h:5002", "h:5003"], "stateful": True},
        ])
        results = set()
        for _ in range(50):
            rid = uuid.uuid4().hex
            results.add(self._resolve_endpoint(redis, "FA", rid))

        # With 50 random picks across 3 endpoints, we should see at least 2
        # (statistically near-certain)
        assert len(results) >= 2, (
            f"Expected at least 2 distinct endpoints across 50 requests, got {results}"
        )

    def test_stateful_binding_persisted_in_redis(self, redis):
        """The affinity binding should exist as a Redis key."""
        _seed_routing_table(redis, [
            {"name": "FA", "endpoints": ["h:5001", "h:5002"], "stateful": True},
        ])
        rid = uuid.uuid4().hex
        chosen = self._resolve_endpoint(redis, "FA", rid)

        affinity_key = f"affinity:{rid}"
        assert redis.hget(affinity_key, "FA") == chosen

    def test_stateless_no_affinity_key(self, redis):
        """Stateless agents should NOT create affinity keys in Redis."""
        _seed_routing_table(redis, [
            {"name": "SL", "endpoints": ["h:6001", "h:6002", "h:6003"]},
        ])
        rid = uuid.uuid4().hex
        self._resolve_endpoint(redis, "SL", rid)

        affinity_key = f"affinity:{rid}"
        assert redis.hget(affinity_key, "SL") is None

    def test_stateless_returns_valid_endpoint(self, redis):
        """Stateless agents should always return one of the available endpoints."""
        _seed_routing_table(redis, [
            {"name": "SL", "endpoints": ["h:6001", "h:6002"]},
        ])
        for _ in range(20):
            result = self._resolve_endpoint(redis, "SL", uuid.uuid4().hex)
            assert result in ["h:6001", "h:6002"]

    def test_unknown_service_returns_none(self, redis):
        """Looking up a service not in the routing table should return None."""
        _seed_routing_table(redis, [
            {"name": "AgentA", "endpoints": ["h:5001"]},
        ])
        assert self._resolve_endpoint(redis, "NonExistent", uuid.uuid4().hex) is None

    def test_empty_endpoint_list_returns_none(self, redis):
        """A known service with no endpoints should not route anywhere."""
        _seed_routing_table(redis, [
            {"name": "Empty", "endpoints": []},
        ])

        assert self._resolve_endpoint(redis, "Empty", uuid.uuid4().hex) is None

    def test_stateful_without_request_id_does_not_write_affinity(self, redis):
        """Stateful routing only becomes sticky when a request_id is supplied."""
        _seed_routing_table(redis, [
            {"name": "FA", "endpoints": ["h:5001", "h:5002"], "stateful": True},
        ])

        assert self._resolve_endpoint(redis, "FA", None) in ["h:5001", "h:5002"]
        assert redis.scan_keys("affinity:*") == []

    def test_existing_affinity_binding_is_used_even_if_endpoint_order_changes(self, redis):
        """Existing sticky bindings should win over later endpoint list order."""
        rid = uuid.uuid4().hex
        redis.hset(f"affinity:{rid}", "FA", "h:5002")
        _seed_routing_table(redis, [
            {"name": "FA", "endpoints": ["h:5001", "h:5002"], "stateful": True},
        ])

        assert self._resolve_endpoint(redis, "FA", rid) == "h:5002"


# ------------------------------------------------------------------ #
#  Tests — Cleanup                                                     #
# ------------------------------------------------------------------ #

class TestAffinityCleanup:
    """Verify that affinity keys are cleaned up when a request completes."""

    def test_affinity_keys_cleaned_up(self, redis):
        """After cleanup, affinity:<request_id> keys should be gone."""
        rid = uuid.uuid4().hex
        # Simulate affinity bindings for a request
        redis.hset(f"affinity:{rid}", "AgentA", "h:5001")
        redis.hset(f"affinity:{rid}", "AgentB", "h:6001")

        # Verify they exist
        assert redis.hget(f"affinity:{rid}", "AgentA") == "h:5001"
        assert redis.hget(f"affinity:{rid}", "AgentB") == "h:6001"

        # Simulate cleanup (what _cleanup_request does)
        redis.delete(f"affinity:{rid}")

        # Verify they're gone
        assert redis.hget(f"affinity:{rid}", "AgentA") is None
        assert redis.hget(f"affinity:{rid}", "AgentB") is None

    def test_cleanup_does_not_affect_other_requests(self, redis):
        """Cleaning up one request's affinity should not touch another's."""
        rid_a = uuid.uuid4().hex
        rid_b = uuid.uuid4().hex

        redis.hset(f"affinity:{rid_a}", "Agent", "h:5001")
        redis.hset(f"affinity:{rid_b}", "Agent", "h:5002")

        # Clean up only rid_a
        redis.delete(f"affinity:{rid_a}")

        # rid_a gone, rid_b untouched
        assert redis.hget(f"affinity:{rid_a}", "Agent") is None
        assert redis.hget(f"affinity:{rid_b}", "Agent") == "h:5002"


# ------------------------------------------------------------------ #
#  Tests — scan_keys utility                                           #
# ------------------------------------------------------------------ #

class TestScanKeys:
    """Verify the scan_keys helper on RedisClient."""

    def test_scan_keys_finds_matching(self, redis):
        """scan_keys should return all keys matching a glob pattern."""
        prefix = uuid.uuid4().hex[:8]
        redis.set(f"test:{prefix}:a", "1")
        redis.set(f"test:{prefix}:b", "2")
        redis.set(f"test:{prefix}:c", "3")
        redis.set(f"test:other:d", "4")

        keys = redis.scan_keys(f"test:{prefix}:*")
        assert len(keys) == 3
        assert f"test:{prefix}:a" in keys
        assert f"test:{prefix}:b" in keys
        assert f"test:{prefix}:c" in keys

        # Cleanup
        redis.delete(f"test:{prefix}:a", f"test:{prefix}:b", f"test:{prefix}:c", f"test:other:d")

    def test_scan_keys_returns_empty_for_no_match(self, redis):
        """scan_keys should return an empty list when nothing matches."""
        keys = redis.scan_keys("nonexistent_pattern_xyz:*")
        assert keys == []


class _FakeRedis:
    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            self.sets.pop(key, None)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

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


class _FakeController:
    def __init__(self):
        self.redis = _FakeRedis()
        self.node_redis = {}


def _write_instance(manager, agent_name, replica_index, host_port):
    instance = {
        "agent_name": agent_name,
        "provider": "local",
        "replica_index": str(replica_index),
        "host": "localhost",
        "host_port": str(host_port),
        "container_port": "50051",
        "endpoint": f"localhost:{host_port}",
        "redis_host": "host.docker.internal",
        "redis_port": "6379",
        "runtime_id": f"ventis-local-{agent_name.lower()}-{replica_index}",
    }
    manager._write_instance(instance)
    manager.redis.sadd(
        f"agent:{agent_name}:instances",
        manager._instance_id_from_record(instance),
    )


class TestRoutingSnapshot:
    def test_publish_routing_snapshot_orders_replicas_and_marks_stateful(self):
        controller = _FakeController()
        manager = InstanceManager(controller, controller.redis)
        _write_instance(manager, "Alpha", 1, 8001)
        _write_instance(manager, "Alpha", 0, 8000)

        manager.publish_routing_snapshot([{"name": "Alpha", "stateful": True}])

        assert json.loads(controller.redis.hget(ROUTING_ENDPOINTS_KEY, "Alpha")) == [
            "host.docker.internal:8000",
            "host.docker.internal:8001",
        ]
        assert controller.redis.hget(ROUTING_STATEFUL_KEY, "Alpha") == "true"
        assert controller.redis.smembers("routing_table:services") == {"Alpha"}

    def test_publish_routing_snapshot_clears_stale_service_metadata(self):
        controller = _FakeController()
        manager = InstanceManager(controller, controller.redis)

        controller.redis.sadd("routing_table:services", "Old", "Keep")
        controller.redis.hset(ROUTING_ENDPOINTS_KEY, "Old", json.dumps(["localhost:9000"]))
        controller.redis.hset(ROUTING_STATEFUL_KEY, "Old", "true")
        controller.redis.hset(ROUTING_STATEFUL_KEY, "Keep", "true")

        manager.publish_routing_snapshot([{"name": "Keep", "stateful": False}])

        assert controller.redis.smembers("routing_table:services") == {"Keep"}
        assert controller.redis.hget(ROUTING_ENDPOINTS_KEY, "Old") is None
        assert controller.redis.hget(ROUTING_STATEFUL_KEY, "Old") is None
        assert controller.redis.hget(ROUTING_STATEFUL_KEY, "Keep") is None
