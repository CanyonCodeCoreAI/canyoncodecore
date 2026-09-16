import subprocess

from canyonos import docker_cmd


def test_run_docker_reports_timeout(monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(kwargs["timeout"], args[0])

    monkeypatch.setattr(docker_cmd.subprocess, "run", timed_out)
    try:
        docker_cmd.run_docker(["docker", "info"], timeout=3, action="Checking Docker")
    except RuntimeError as exc:
        assert str(exc) == "Checking Docker timed out after 3s."
    else:
        raise AssertionError("expected timeout error")


def test_cleanup_retries_once_and_returns_final_failure(monkeypatch):
    calls = []

    def failed(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 1, "", "busy")

    monkeypatch.setattr(docker_cmd.subprocess, "run", failed)
    monkeypatch.setattr(docker_cmd.time, "sleep", lambda _: None)

    error = docker_cmd.cleanup_docker(
        ["docker", "rm", "x"], action="Removing x", timeout=7
    )

    assert error == "Removing x failed: busy"
    assert len(calls) == 2
    assert all(timeout == 7 for _, timeout in calls)

