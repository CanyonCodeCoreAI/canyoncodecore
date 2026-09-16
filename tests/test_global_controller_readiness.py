from types import SimpleNamespace

import pytest

from canyonos_core.controller.global_controller import GlobalController


class _Redis:
    def get(self, _key):
        return None


def test_unhealthy_replicas_fail_startup_without_entering_the_run_loop(monkeypatch):
    controller = GlobalController.__new__(GlobalController)
    controller.instance_manager = SimpleNamespace(
        list_instances=lambda: [
            {
                "agent_name": "BrokenAgent",
                "host": "127.0.0.1",
                "host_port": 50051,
            }
        ],
        _routing_endpoint_for=lambda instance: (
            f"{instance['host']}:{instance['host_port']}"
        ),
    )
    controller.node_redis = {}
    controller.redis = _Redis()
    controller._last_status = {}

    with pytest.raises(RuntimeError, match="failed to become healthy.*BrokenAgent"):
        controller._wait_for_healthy(timeout=0, interval=0)
