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
    targets = [(instance, redis_resolver(instance["host"])) for instance in instances]
    poller = TelemetryPoller(targets_provider=lambda: targets, **kwargs)
    return poller


class TelemetryPollerTests(unittest.TestCase):
    def test_polls_instances_concurrently(self):
        sleep = 0.15
        instances = [
            {"agent_name": f"Agent{i}", "host": f"host{i}", "host_port": 50051 + i}
            for i in range(5)
        ]
        redis_by_host = {instance["host"]: _FakeRedis() for instance in instances}
        poller = _poller(instances, redis_by_host.__getitem__)

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
            poller._poll_once()
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
        poller = _poller(
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
            poller._poll_once()

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
        poller = _poller([instance], lambda host: redis)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"), patch(
            "ventis.controller.telemetry_poller.send_agent_information",
            side_effect=RuntimeError("database unavailable"),
        ):
            poller._poll_once()

        self.assertEqual(redis.writes, [])

    def test_one_instance_erroring_does_not_block_others(self):
        instances = [
            {"agent_name": "Bad", "host": "bad-host", "host_port": 50051},
            {"agent_name": "Good", "host": "good-host", "host_port": 50052},
        ]
        bad_redis, good_redis = _FakeRedis(), _FakeRedis()
        poller = _poller(
            instances, {"bad-host": bad_redis, "good-host": good_redis}.__getitem__
        )

        polled = []

        def _pull(node_redis):
            if node_redis is bad_redis:
                raise RuntimeError("boom")
            return []

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            side_effect=_pull,
        ), patch(
            "ventis.controller.telemetry_poller.send_runtime_information",
            side_effect=lambda rows, redis_client, database_url: polled.append(
                redis_client
            ),
        ):
            poller._poll_once()  # must not raise

        self.assertEqual(polled, [good_redis])

    def test_runs_on_its_own_timer_without_any_external_trigger(self):
        # The whole point of the poller owning its own thread: it must poll
        # repeatedly on its own schedule, with nothing else driving it.
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        poll_count = threading.Event()
        calls = []
        poller = _poller([instance], lambda host: _FakeRedis(), poll_interval=0.05)

        def pull(redis_client):
            calls.append(time.monotonic())
            if len(calls) >= 3:
                poll_count.set()
            return []

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            side_effect=pull,
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"):
            poller.start()
            try:
                self.assertTrue(poll_count.wait(2), "poller never reached 3 cycles on its own")
            finally:
                poller.stop()

        self.assertGreaterEqual(len(calls), 3)

    def test_start_is_idempotent(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        poller = _poller([instance], lambda host: _FakeRedis(), poll_interval=30)

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            return_value=[],
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"):
            self.assertTrue(poller.start())
            self.assertFalse(poller.start())
            poller.stop()

    def test_stop_interrupts_interval_wait_promptly(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        first_cycle = threading.Event()
        poller = _poller([instance], lambda host: _FakeRedis(), poll_interval=30)

        def pull(redis_client):
            first_cycle.set()
            return []

        with patch(
            "ventis.controller.telemetry_poller.pull_runtime_information",
            side_effect=pull,
        ), patch("ventis.controller.telemetry_poller.send_runtime_information"):
            poller.start()
            self.assertTrue(first_cycle.wait(1))

            started = time.monotonic()
            self.assertTrue(poller.stop())

        self.assertLess(time.monotonic() - started, 0.5)

    def test_stop_is_bounded_when_database_write_is_blocked(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        entered = threading.Event()
        release = threading.Event()
        poller = _poller([instance], lambda host: _FakeRedis(), poll_interval=30)

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
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            try:
                self.assertFalse(poller.stop(timeout=0.05))
                self.assertLess(time.monotonic() - started, 0.3)
            finally:
                release.set()
                self.assertTrue(poller.stop(timeout=1))

    def test_update_settings_is_used_on_the_next_cycle(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        key = "controller:host:50051:metrics"
        redis = _FakeRedis({key: {"requests_served": "10"}})
        poller = _poller(
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
            poller._poll_once()

        self.assertEqual(runtime.call_args.args[2], "second-db")
        self.assertEqual(sent_urls, ["second-db"])

    def test_no_targets_is_a_noop(self):
        poller = TelemetryPoller(targets_provider=lambda: [])
        poller._poll_once()  # must not raise


if __name__ == "__main__":
    unittest.main()
