import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs")))

from ventis.controller.global_controller import GlobalController


class _FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, name):
        return self.values.get(name)


class _FakeInstanceManager:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self):
        return self._instances

    def _instance_id_from_record(self, instance):
        return instance["agent_name"]

    def remove_instance(self, instance_id):
        pass


class _FakeTelemetryPoller:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()

    def start(self):
        self.started.set()
        return True

    def stop(self, timeout=5):
        self.stopped.set()
        return True


def _bare_controller(instances, redis=None):
    """Build a GlobalController without running its heavy __init__."""
    controller = GlobalController.__new__(GlobalController)
    controller.redis = redis or _FakeRedis()
    controller.node_redis = {}
    controller.poll_interval = 30
    controller.cleanup_interval = 30
    controller._last_status = {}
    controller.instance_manager = _FakeInstanceManager(instances)
    controller._shutdown_event = threading.Event()
    controller._lifecycle_lock = threading.Lock()
    controller._run_thread = None
    controller.running = False
    controller.telemetry_poller = _FakeTelemetryPoller()
    controller._cleanup_thread = None
    controller.containers = {}
    controller.redis_containers = {}
    controller.controllers = []
    return controller


class HealthTelemetrySplitTests(unittest.TestCase):
    def test_telemetry_targets_matches_instances_and_their_node_redis(self):
        instances = [
            {"agent_name": "A", "host": "host1", "host_port": 50051},
            {"agent_name": "B", "host": "host2", "host_port": 50052},
        ]
        redis1, redis2 = _FakeRedis(), _FakeRedis()
        controller = _bare_controller(instances)
        controller.node_redis = {"host1": redis1, "host2": redis2}

        targets = controller._telemetry_targets()

        self.assertEqual(
            targets, [(instances[0], redis1), (instances[1], redis2)]
        )

    def test_run_starts_telemetry_poller_immediately_and_stop_tears_it_down(self):
        controller = _bare_controller([])

        thread = threading.Thread(target=controller.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(
                controller.telemetry_poller.started.wait(1),
                "telemetry poller was not started by run()",
            )
        finally:
            started = time.monotonic()
            controller.stop()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(controller.telemetry_poller.stopped.is_set())
        # Health loop uses event-driven waits, so shutdown must not block on
        # the full poll_interval (30s here).
        self.assertLess(time.monotonic() - started, 2)

    def test_health_loop_failure_does_not_affect_telemetry_poller(self):
        # A health-check exception must not prevent telemetry from having
        # already been started -- the two are on independent threads.
        instances = [{"agent_name": "A", "host": "bad-host", "host_port": 50051}]
        controller = _bare_controller(instances)

        def _boom(instance, node_redis):
            raise RuntimeError("health check exploded")

        controller._poll_instance_health = _boom

        thread = threading.Thread(target=controller.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(controller.telemetry_poller.started.wait(1))
        finally:
            controller.stop()
            thread.join(timeout=2)

        self.assertTrue(controller.telemetry_poller.stopped.is_set())

    def test_run_is_idempotent(self):
        controller = _bare_controller([])
        thread = threading.Thread(target=controller.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(controller.telemetry_poller.started.wait(1))
            # A second run() call while already running must return immediately.
            controller.telemetry_poller.started.clear()
            controller.run()
            self.assertFalse(controller.telemetry_poller.started.is_set())
        finally:
            controller.stop()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
