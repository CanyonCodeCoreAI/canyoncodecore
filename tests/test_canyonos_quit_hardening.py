import subprocess

from canyonos import docker_cmd, quit as quit_cmd
from canyonos.gc import GCError


def _state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"container_id": "abcdef123456", "port": 8000}')
    monkeypatch.setattr(quit_cmd, "STATE_PATH", str(state_path))
    monkeypatch.setattr(
        quit_cmd,
        "require_state",
        lambda: {"container_id": "abcdef123456", "port": 8000},
    )
    monkeypatch.setattr(quit_cmd, "_container_state", lambda _container_id: (True, True))
    monkeypatch.setattr(quit_cmd, "teardown_dashboard", lambda: True)
    return state_path


def test_controller_cleanup_failure_preserves_state(monkeypatch, tmp_path):
    state_path = _state(monkeypatch, tmp_path)
    docker_calls = []
    monkeypatch.setattr(
        quit_cmd,
        "post_clean",
        lambda _port: (_ for _ in ()).throw(GCError("controller stuck", code=500)),
    )

    def docker(argv, **_kwargs):
        docker_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_cmd.subprocess, "run", docker)

    failures = quit_cmd.run_quit()

    assert any("controller stuck" in failure for failure in failures)
    assert state_path.exists()
    # A controller that won't clean doesn't stop the rest of the teardown.
    assert ["docker", "rm", "abcdef123456"] in docker_calls


def test_docker_cleanup_failure_preserves_state(monkeypatch, tmp_path):
    state_path = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(quit_cmd, "post_clean", lambda _port: None)

    def docker(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            1 if argv[:2] == ["docker", "rm"] else 0,
            "",
            "daemon error" if argv[:2] == ["docker", "rm"] else "",
        )

    monkeypatch.setattr(docker_cmd.subprocess, "run", docker)

    failures = quit_cmd.run_quit()

    assert any("daemon error" in failure for failure in failures)
    assert state_path.exists()


def test_successful_cleanup_removes_state(monkeypatch, tmp_path):
    state_path = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(quit_cmd, "post_clean", lambda _port: None)
    monkeypatch.setattr(
        docker_cmd.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    assert quit_cmd.run_quit() == []

    assert not state_path.exists()


def test_stopped_controller_is_removed_without_calling_clean(monkeypatch, tmp_path):
    state_path = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(quit_cmd, "_container_state", lambda _container_id: (True, False))
    clean_calls = []
    docker_calls = []
    monkeypatch.setattr(quit_cmd, "post_clean", lambda _port: clean_calls.append(True))

    def docker(argv, **_kwargs):
        docker_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_cmd.subprocess, "run", docker)

    assert quit_cmd.run_quit() == []

    assert clean_calls == []
    assert ["docker", "stop", "abcdef123456"] not in docker_calls
    assert ["docker", "rm", "abcdef123456"] in docker_calls
    assert not state_path.exists()


def test_dashboard_teardown_failure_preserves_state(monkeypatch, tmp_path):
    state_path = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(quit_cmd, "post_clean", lambda _port: None)
    monkeypatch.setattr(quit_cmd, "teardown_dashboard", lambda: False)
    monkeypatch.setattr(
        docker_cmd.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    failures = quit_cmd.run_quit()

    assert any("dashboard stack failed" in failure for failure in failures)
    assert state_path.exists()
