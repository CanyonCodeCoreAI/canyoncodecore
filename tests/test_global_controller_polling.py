import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs")))

from ventis.controller.global_controller import GlobalController


class _FakeRedis:
    """hgetall/get always return "nothing new" so only send_runtime_information
    (the thing under test) is on the hot path -- send_agent_information's branch
    is skipped entirely since `if metrics:` is false for an empty hgetall.
    """

    def hgetall(self, name):
        return {}

    def get(self, name):
        return None


class _FakeInstanceManager:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self, agent_name=None):
        return self._instances


def _bare_controller(instances):
    controller = GlobalController.__new__(GlobalController)
    controller.redis = _FakeRedis()
    controller.node_redis = {}
    controller.instance_manager = _FakeInstanceManager(instances)
    controller.config = {}
    controller.poll_interval = 5
    controller._last_metrics_poll_time = {}
    controller._last_status = {}
    return controller


class PollControllersConcurrencyTests(unittest.TestCase):
    def test_polls_all_instances_concurrently_not_serially(self):
        # Each instance's send_runtime_information call sleeps, simulating a slow
        # Postgres round trip. If _poll_controllers ran instances one at a time,
        # total wall time would scale with instance count (N x SLEEP); polled
        # concurrently, it should stay close to a single SLEEP regardless of N.
        SLEEP = 0.2
        instances = [
            {"agent_name": f"Agent{i}", "host": f"host{i}", "host_port": 50051 + i}
            for i in range(5)
        ]
        controller = _bare_controller(instances)

        def _slow_send_runtime_information(rows, redis_client, database_url):
            time.sleep(SLEEP)

        with patch(
            "ventis.controller.global_controller.send_runtime_information",
            side_effect=_slow_send_runtime_information,
        ), patch(
            "ventis.controller.global_controller.pull_runtime_information",
            return_value=[],
        ):
            start = time.monotonic()
            controller._poll_controllers()
            elapsed = time.monotonic() - start

        self.assertLess(
            elapsed,
            SLEEP * len(instances) / 2,
            f"expected concurrent polling (~{SLEEP}s), took {elapsed:.2f}s for "
            f"{len(instances)} instances -- looks serial",
        )

    def test_one_instance_erroring_does_not_block_others(self):
        # pull_runtime_information() is called *inside* _poll_instance's try block
        # (as an argument to send_runtime_information), so a raise from one
        # instance's pull must not stop send_runtime_information from running for
        # every other instance being polled concurrently.
        instances = [
            {"agent_name": "Bad", "host": "bad-host", "host_port": 50051},
            {"agent_name": "Good", "host": "good-host", "host_port": 50052},
        ]
        controller = _bare_controller(instances)
        bad_redis, good_redis = _FakeRedis(), _FakeRedis()
        controller.node_redis = {"bad-host": bad_redis, "good-host": good_redis}

        polled = []

        def _pull(node_redis):
            if node_redis is bad_redis:
                raise RuntimeError("boom")
            return []

        with patch(
            "ventis.controller.global_controller.pull_runtime_information",
            side_effect=_pull,
        ), patch(
            "ventis.controller.global_controller.send_runtime_information",
            side_effect=lambda rows, redis_client, database_url: polled.append(
                redis_client
            ),
        ):
            controller._poll_controllers()  # must not raise

        self.assertEqual(polled, [good_redis])


if __name__ == "__main__":
    unittest.main()
