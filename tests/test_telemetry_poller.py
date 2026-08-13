import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.telemetry_poller import TelemetryPoller


class _FakeRedis:
    def __init__(self, metrics=None):
        self.metrics = metrics or {}
        self.writes = []

    def hgetall(self, key):
        return dict(self.metrics.get(key, {}))

    def hset_multiple(self, key, values):
        self.writes.append((key, values))
        self.metrics.setdefault(key, {}).update(values)


def _poller(instances, redis_resolver, **kwargs):
    poller = TelemetryPoller(**kwargs)
    targets = [(instance, redis_resolver(instance["host"])) for instance in instances]
    return poller, targets


class TelemetryPollerTests(unittest.TestCase):
    def test_polls_instances_concurrently(self):
        sleep = 0.15
        instances = [
            {"agent_name": f"Agent{i}", "host": f"host{i}", "host_port": 50051 + i}
            for i in range(5)
        ]
        redis_by_host = {instance["host"]: _FakeRedis() for instance in instances}
        poller, targets = _poller(instances, redis_by_host.__getitem__)

        def slow_send(rows, redis_client, database_url):
            time.sleep(sleep)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch(
            "ventis.controller.telemetry_poller.send_runtime_information",
            side_effect=slow_send,
        ):
            started = time.monotonic()
            self.assertTrue(poller.poll_once(targets))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, sleep * len(instances) / 2)

    def test_records_throughput_and_resets_counters_after_agent_write(self):
        instance = {"agent_name": "Agent", "host": "localhost", "host_port": 50051}
        key = "controller:host.docker.internal:50051:metrics"
        redis = _FakeRedis(
            {
                key: {
                    "requests_served": "10",
                    "error_count": "2",
                    "full_failures": "1",
                }
            }
        )
        poller, targets = _poller(
            [instance], lambda host: redis, poll_interval=5,
            database_url="sqlite:///telemetry.db",
        )

        sent = []
        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"), patch(
            "ventis.controller.telemetry_poller.send_agent_information",
            side_effect=lambda rows, database_url: sent.extend(rows),
        ), patch("ventis.controller.telemetry_poller.time.time", return_value=100.0):
            self.assertTrue(poller.poll_once(targets))

        self.assertEqual(sent[0]["requests_served"], 10)
        self.assertEqual(sent[0]["throughput"], 2.0)
        self.assertEqual(sent[0]["agent_name"], "Agent")
        self.assertEqual(
            redis.writes,
            [
                (
                    key,
                    {
                        "full_failures": 0,
                        "error_count": 0,
                        "requests_served": 0,
                    },
                )
            ],
        )

    def test_does_not_reset_counters_when_agent_write_fails(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        key = "controller:host:50051:metrics"
        redis = _FakeRedis({key: {"requests_served": "1"}})
        poller, targets = _poller([instance], lambda host: redis)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"), patch(
            "ventis.controller.telemetry_poller.send_agent_information",
            side_effect=RuntimeError("database unavailable"),
        ):
            self.assertTrue(poller.poll_once(targets))

        self.assertEqual(redis.writes, [])

    def test_start_is_idempotent_and_skips_overlapping_manual_cycle(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        entered = threading.Event()
        release = threading.Event()
        poller, targets = _poller([instance], lambda host: _FakeRedis())

        def blocked_send(rows, redis_client, database_url):
            entered.set()
            release.wait(1)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch(
            "ventis.controller.telemetry_poller.send_runtime_information",
            side_effect=blocked_send,
        ):
            self.assertTrue(poller.start())
            poller.request_poll(targets)
            self.assertTrue(entered.wait(1), "background thread did not poll immediately")
            self.assertFalse(poller.start())
            self.assertFalse(poller.poll_once(targets))

            release.set()
            self.assertTrue(poller.stop())

    def test_stop_interrupts_interval_wait_promptly(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        first_cycle = threading.Event()
        poller, targets = _poller(
            [instance], lambda host: _FakeRedis(), poll_interval=30
        )

        def pull(redis_client):
            first_cycle.set()
            return []

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            side_effect=pull,
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"):
            poller.start()
            poller.request_poll(targets)
            self.assertTrue(first_cycle.wait(1))

            started = time.monotonic()
            self.assertTrue(poller.stop())

        self.assertLess(time.monotonic() - started, 0.5)

    def test_worker_waits_for_controller_poll_requests(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        polled = threading.Event()
        poller, targets = _poller([instance], lambda host: _FakeRedis())

        def pull(redis_client):
            polled.set()
            return []

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            side_effect=pull,
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"):
            poller.start()
            try:
                self.assertFalse(polled.wait(0.05))
                poller.request_poll(targets)
                self.assertTrue(polled.wait(1))
            finally:
                poller.stop()

    def test_stop_is_bounded_when_database_write_is_blocked(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        entered = threading.Event()
        release = threading.Event()
        poller, targets = _poller([instance], lambda host: _FakeRedis())

        def blocked_send(rows, redis_client, database_url):
            entered.set()
            release.wait(2)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch(
            "ventis.controller.telemetry_poller.send_runtime_information",
            side_effect=blocked_send,
        ):
            poller.start()
            poller.request_poll(targets)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            try:
                self.assertFalse(poller.stop(timeout=0.05))
                self.assertLess(time.monotonic() - started, 0.3)
            finally:
                release.set()
                self.assertTrue(poller.stop(timeout=1))

    def test_update_settings_is_used_as_a_single_cycle_snapshot(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        key = "controller:host:50051:metrics"
        redis = _FakeRedis({key: {"requests_served": "10"}})
        poller, targets = _poller(
            [instance], lambda host: redis, poll_interval=5,
            database_url="first-db",
        )
        poller.update_settings(10, "second-db")

        sent_urls = []
        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch("ventis.controller.telemetry_poller.send_runtime_information") as runtime, patch(
            "ventis.controller.telemetry_poller.send_agent_information",
            side_effect=lambda rows, database_url: sent_urls.append(database_url),
        ), patch("ventis.controller.telemetry_poller.time.time", return_value=100.0):
            self.assertTrue(poller.poll_once(targets))

        self.assertEqual(runtime.call_args.args[2], "second-db")
        self.assertEqual(sent_urls, ["second-db"])

    def test_reload_during_cycle_does_not_mix_settings(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        key = "controller:host:50051:metrics"
        redis = _FakeRedis({key: {"requests_served": "10"}})
        poller, targets = _poller(
            [instance],
            lambda host: redis,
            poll_interval=5,
            database_url="old-db",
        )
        runtime_entered = threading.Event()
        release_runtime = threading.Event()
        runtime_urls = []
        agent_urls = []

        def blocked_runtime(rows, redis_client, database_url):
            runtime_urls.append(database_url)
            runtime_entered.set()
            release_runtime.wait(1)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch(
            "ventis.controller.telemetry_poller.send_runtime_information",
            side_effect=blocked_runtime,
        ), patch(
            "ventis.controller.telemetry_poller.send_agent_information",
            side_effect=lambda rows, database_url: agent_urls.append(database_url),
        ):
            cycle = threading.Thread(target=lambda: poller.poll_once(targets))
            cycle.start()
            self.assertTrue(runtime_entered.wait(1))
            poller.update_settings(11, "new-db")
            release_runtime.set()
            cycle.join(1)
            self.assertFalse(cycle.is_alive())

            poller.poll_once(targets)

        self.assertEqual(runtime_urls, ["old-db", "new-db"])
        self.assertEqual(agent_urls, ["old-db", "new-db"])


if __name__ == "__main__":
    unittest.main()
