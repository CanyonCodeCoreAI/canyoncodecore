import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))
)

from ventis.controller.global_controller import GlobalController


class _InstanceManager:
    def __init__(self, instances=()):
        self.instances = list(instances)

    def list_instances(self):
        return list(self.instances)


def _bare_controller(instances=()):
    controller = GlobalController.__new__(GlobalController)
    controller.running = False
    controller.poll_interval = 0.01
    controller.cleanup_interval = 60
    controller._shutdown_event = threading.Event()
    controller._lifecycle_lock = threading.Lock()
    controller._run_thread = None
    controller._cleanup_thread = None
    controller._last_status = {}
    controller.instance_manager = _InstanceManager(instances)
    controller.telemetry_poller = MagicMock()
    controller.telemetry_poller.stop.return_value = True
    controller._trigger_cleanup = MagicMock()
    controller._stop_docker_agents = MagicMock()
    controller._stop_redis_containers = MagicMock()
    return controller


class GlobalControllerLifecycleTests(unittest.TestCase):
    def test_run_blocks_in_health_loop_and_starts_workers_only_once(self):
        controller = _bare_controller()
        run_thread = threading.Thread(target=controller.run, daemon=True)

        run_thread.start()
        deadline = time.monotonic() + 0.5
        while (
            not controller.telemetry_poller.request_poll.called
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        cleanup_thread = controller._cleanup_thread

        self.assertTrue(run_thread.is_alive())
        controller.run()

        controller.telemetry_poller.request_poll.assert_called()
        self.assertEqual(
            controller.telemetry_poller.request_poll.call_args.args[0], []
        )
        controller.telemetry_poller.start.assert_called_once_with()
        self.assertIs(controller._cleanup_thread, cleanup_thread)
        controller.stop()
        run_thread.join(0.5)
        self.assertFalse(run_thread.is_alive())

    def test_health_transitions_continue_while_telemetry_is_blocked(self):
        instance = {"agent_name": "Agent", "host": "host", "host_port": 50051}
        controller = _bare_controller([instance])
        healthy = threading.Event()
        telemetry_blocked = threading.Event()
        release_telemetry = threading.Event()

        class _BlockedTelemetry:
            def request_poll(self, targets):
                pass

            def start(self):
                def _block():
                    telemetry_blocked.set()
                    release_telemetry.wait()

                threading.Thread(target=_block, daemon=True).start()

            def stop(self, timeout=5):
                release_telemetry.set()
                return True

        class _Redis:
            def get(self, key):
                return "healthy"

        controller.telemetry_poller = _BlockedTelemetry()
        controller._get_node_redis_for = lambda host: _Redis()
        controller._agent_host_key = lambda host: host
        controller._on_controller_healthy = lambda *args: healthy.set()
        controller._on_controller_unhealthy = MagicMock()

        run_thread = threading.Thread(target=controller.run, daemon=True)
        run_thread.start()

        self.assertTrue(telemetry_blocked.wait(0.5))
        self.assertTrue(healthy.wait(0.5))
        controller.stop()
        run_thread.join(0.5)

    def test_stop_joins_workers_before_container_teardown(self):
        controller = _bare_controller()
        controller.running = True
        calls = []

        class _Worker:
            def __init__(self, name):
                self.name = name

            def join(self, timeout=None):
                calls.append((self.name, "join", timeout))

            def is_alive(self):
                return False

        controller._run_thread = _Worker("health")
        controller._cleanup_thread = _Worker("cleanup")
        controller.telemetry_poller.stop.side_effect = lambda timeout: (
            calls.append(("telemetry", "stop", timeout)) or True
        )
        controller._stop_docker_agents.side_effect = lambda: calls.append(
            ("agents", "stop")
        )
        controller._stop_redis_containers.side_effect = lambda: calls.append(
            ("redis", "stop")
        )

        controller.stop()

        self.assertEqual(
            calls,
            [
                ("health", "join", 5),
                ("telemetry", "stop", 5),
                ("cleanup", "join", 5),
                ("agents", "stop"),
                ("redis", "stop"),
            ],
        )

    def test_teardown_continues_after_telemetry_join_timeout(self):
        controller = _bare_controller()
        controller.telemetry_poller.stop.return_value = False

        with patch("ventis.controller.global_controller.logger.warning") as warning:
            controller.stop()

        controller._stop_docker_agents.assert_called_once_with()
        controller._stop_redis_containers.assert_called_once_with()
        self.assertTrue(
            any("Telemetry poller did not stop" in call.args[0] for call in warning.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
