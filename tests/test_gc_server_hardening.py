import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from canyonos_core import server


def test_clean_has_a_bounded_wait(monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("controller", 45)
    monkeypatch.setattr(server, "_gc_process", process)

    response = server.app.test_client().post("/clean")

    assert response.status_code == 504
    assert "did not stop" in response.get_json()["error"]
    process.wait.assert_called_once_with(timeout=server.CLEAN_TIMEOUT_SECONDS)
    assert server._gc_process is process


def test_concurrent_deploy_requests_start_only_one_process(monkeypatch, tmp_path):
    config = tmp_path / "global_controller.yaml"
    config.write_text("agents: []\n")
    calls = []

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.05)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(server, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_gc_process", None)
    monkeypatch.setattr(server.subprocess, "Popen", popen)

    def request_deploy():
        with server.app.test_client() as client:
            return client.post(
                "/deploy", json={"config_path": "global_controller.yaml"}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _index: request_deploy(), range(2)))

    assert statuses == [200, 409]
    assert len(calls) == 1
