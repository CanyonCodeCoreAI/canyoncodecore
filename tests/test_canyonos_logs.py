import subprocess

import pytest

from canyonos import logs


def test_unreachable_controller_fails_logs(monkeypatch):
    monkeypatch.setattr(
        logs, "require_state", lambda: {"container_id": "abc", "port": 8000}
    )
    monkeypatch.setattr(logs, "deploy_status", lambda _port: None)

    with pytest.raises(RuntimeError, match="Could not reach"):
        logs.run_logs()


def test_failed_docker_log_stream_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(
        logs, "require_state", lambda: {"container_id": "abc", "port": 8000}
    )
    monkeypatch.setattr(logs, "deploy_status", lambda _port: {"running": True})
    monkeypatch.setattr(
        logs.subprocess,
        "run",
        lambda argv: subprocess.CompletedProcess(argv, 17),
    )

    with pytest.raises(RuntimeError, match="exit code 17"):
        logs.run_logs()
