import json
import subprocess
from pathlib import Path

import pytest

from canyonos import dashboard_stack


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture
def project(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    for key in (
        "JWT_SECRET",
        "CANYONOS_JWT_SECRET",
        "CANYONOS_REDIS_HOST",
        "CANYONOS_REDIS_PORT",
        "CANYONOS_API_IMAGE",
        "CANYONOS_WEB_IMAGE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dashboard_stack.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(dashboard_stack, "_port_is_free", lambda _port: True)
    return tmp_path


def install_docker(monkeypatch, calls, responses=None):
    responses = responses or {}

    def fake_run(argv, **_):
        calls.append(argv)
        if callable(responses):
            return responses(argv)
        for marker, result in responses.items():
            if argv[-len(marker) :] == list(marker):
                return result(argv)
        if argv[:3] == ["docker", "container", "inspect"]:
            return completed(argv, returncode=1)
        return completed(argv)

    monkeypatch.setattr(dashboard_stack.subprocess, "run", fake_run)


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (lambda monkeypatch, _: monkeypatch.setattr(dashboard_stack.shutil, "which", lambda _: None), "docker is not on PATH"),
        (
            lambda _, responses: responses.update(
                {("info",): lambda argv: completed(argv, returncode=1)}
            ),
            "docker daemon or socket is unavailable",
        ),
        (
            lambda _, responses: responses.update(
                {("compose", "version"): lambda argv: completed(argv, returncode=1)}
            ),
            "docker compose is unavailable",
        ),
    ],
)
def test_docker_validation_failures_do_not_pull(monkeypatch, project, prepare, message):
    calls = []
    responses = {}
    prepare(monkeypatch, responses)
    install_docker(monkeypatch, calls, responses)

    result = dashboard_stack.run_dashboard()

    assert result == dashboard_stack.ServeResult(False, "validate", message)
    assert all(command[-1] != "pull" for command in calls)







def test_user_jwt_secret_is_untouched_while_canyonos_secret_is_stable(project):
    source_line = "JWT_SECRET=user-value\n"
    Path.cwd().joinpath(".env").write_text(source_line)
    stack = dashboard_stack.DashboardStack(dashboard_stack._state_dir(), Path.cwd())

    first_env, _ = dashboard_stack.prepare(stack)
    second_env, _ = dashboard_stack.prepare(stack)

    env_contents = stack.env_path.read_text()
    assert env_contents.startswith(source_line)
    assert first_env["CANYONOS_JWT_SECRET"] != "user-value"
    assert first_env["CANYONOS_JWT_SECRET"] == second_env["CANYONOS_JWT_SECRET"]




def test_state_directory_and_port_validation_failures_do_not_pull(monkeypatch, project, tmp_path):
    calls = []
    install_docker(monkeypatch, calls)
    blocked_state_dir = tmp_path / "blocked"
    blocked_state_dir.write_text("not a directory")
    monkeypatch.setattr(dashboard_stack, "_state_dir", lambda: blocked_state_dir)

    state_result = dashboard_stack.run_dashboard()

    assert state_result.message == "dashboard state directory is not writable"
    assert all(command[-1] != "pull" for command in calls)

    calls.clear()
    monkeypatch.setattr(dashboard_stack, "_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(dashboard_stack, "_find_web_port", lambda start=8080, max_attempts=50: (_ for _ in ()).throw(
        dashboard_stack.PhaseFailure("validate", "no free port found for the dashboard after 50 attempts starting at 8080")
    ))
    port_result = dashboard_stack.run_dashboard()

    assert port_result.message == "no free port found for the dashboard after 50 attempts starting at 8080"
    assert all(command[-1] != "pull" for command in calls)


def test_prepare_preserves_unrelated_env_lines_and_mode(project):
    Path.cwd().joinpath(".env").write_text(
        "OTHER=one\n# preserved\nJWT_SECRET=kept-secret\nLAST=two\n"
    )
    stack = dashboard_stack.DashboardStack(dashboard_stack._state_dir(), Path.cwd())

    managed_env, message = dashboard_stack.prepare(stack)

    assert message == "dashboard state prepared"
    env_lines = stack.env_path.read_text().splitlines()
    assert env_lines[:2] == ["OTHER=one", "# preserved"]
    assert env_lines[2] == "JWT_SECRET=kept-secret"
    assert env_lines[3] == "LAST=two"
    assert {line.split("=", 1)[0] for line in env_lines if "=" in line} == {
        "OTHER",
        "JWT_SECRET",
        "LAST",
        "CANYONOS_JWT_SECRET",
        "CANYONOS_REDIS_HOST",
        "CANYONOS_REDIS_PORT",
        "CANYONOS_API_IMAGE",
        "CANYONOS_WEB_IMAGE",
        "CANYONOS_WEB_PORT",
    }
    assert stack.env_path.stat().st_mode & 0o777 == 0o600
    assert stack.state_dir.stat().st_mode & 0o777 == 0o700
    assert sorted(path.name for path in stack.state_dir.iterdir()) == ["stack.json"]



def test_redaction_removes_urls_secrets_and_credentials():
    database_url = "postgres://user:password@db.example/canyonos"
    secret = "secret-value"
    logs = f"{database_url}\n{secret}\nredis://other:credential@cache:6379/0"

    redacted = dashboard_stack.redact_logs(logs, secret)

    assert database_url not in redacted  # credentials portion is stripped by the generic regex
    assert secret not in redacted
    assert "user:password@" not in redacted
    assert "other:credential@" not in redacted


@pytest.mark.parametrize("had_containers", [False, True])
def test_start_failure_saves_log_and_cleans_up_only_new_stack(monkeypatch, project, had_containers):
    calls = []

    def response(argv):
        if argv[-2:] == ["ps", "-q"]:
            return completed(argv, stdout="existing\n" if had_containers else "")
        if argv[-5:] == ["up", "-d", "--wait", "--wait-timeout", "180"]:
            return completed(argv, returncode=1)
        if argv[-4:] == ["logs", "--no-color", "--tail", "200"]:
            return completed(argv, stdout="postgres://user:password@db.example/canyonos")
        return completed(argv)

    install_docker(monkeypatch, calls, response)
    result = dashboard_stack.run_dashboard()

    assert result.ok is False
    assert result.phase == "start"
    assert result.log_path is not None
    assert Path(result.log_path).stat().st_mode & 0o777 == 0o600
    assert "postgres://user:password@db.example/canyonos" not in Path(result.log_path).read_text()
    assert any("logs" in command for command in calls)
    assert any(command[-1] == "down" for command in calls) is (not had_containers)


def test_pull_failure_includes_redacted_stderr(monkeypatch, project):
    calls = []
    secret = None

    def response(argv):
        nonlocal secret
        if argv[-1] == "pull":
            secret = next(
                line.split("=", 1)[1]
                for line in Path.cwd().joinpath(".env").read_text().splitlines()
                if line.startswith("CANYONOS_JWT_SECRET=")
            )
            return completed(
                argv,
                returncode=1,
                stderr=f"first line\npull unauthorized postgres://user:password@db.example/canyonos {secret}\n",
            )
        return completed(argv)

    install_docker(monkeypatch, calls, response)
    result = dashboard_stack.run_dashboard()

    assert result.phase == "pull"
    assert "pull unauthorized" in result.message
    assert "postgres://user:password@db.example/canyonos" not in result.message
    assert "user:password@" not in result.message
    assert secret not in result.message


def test_verify_failure_saves_a_log(monkeypatch, project):
    calls = []
    install_docker(monkeypatch, calls)
    probes = []

    def urlopen(*_args, **_kwargs):
        probes.append(True)
        raise dashboard_stack.urllib.error.URLError("down")

    monkeypatch.setattr(
        dashboard_stack.urllib.request,
        "urlopen",
        urlopen,
    )
    clock = iter([0, 0, 0, 31])
    monkeypatch.setattr(dashboard_stack.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(dashboard_stack.time, "sleep", lambda _: None)

    result = dashboard_stack.run_dashboard()

    assert result.ok is False
    assert result.phase == "verify"
    assert result.log_path is not None
    assert Path(result.log_path).is_file()
    assert probes == [True]
    assert any("logs" in command for command in calls)
    assert any(command[-1] == "down" for command in calls)


def test_success_pulls_starts_and_verifies(monkeypatch, project):
    calls = []
    install_docker(monkeypatch, calls)

    class Response:
        status = 200

        def close(self):
            pass

    endpoints = []

    def urlopen(endpoint, timeout):
        endpoints.append((endpoint, timeout))
        return Response()

    monkeypatch.setattr(dashboard_stack.urllib.request, "urlopen", urlopen)
    result = dashboard_stack.run_dashboard()

    assert result == dashboard_stack.ServeResult(
        True, "verify", "dashboard health checks passed", "http://127.0.0.1:8080"
    )
    pull_index = next(index for index, command in enumerate(calls) if command[-1] == "pull")
    up_index = next(index for index, command in enumerate(calls) if "up" in command)
    assert pull_index < up_index
    assert calls[pull_index][4:6] == ["--env-file", str(project / ".env")]
    assert calls[up_index][-5:] == ["up", "-d", "--wait", "--wait-timeout", "180"]
    assert endpoints == [
        ("http://127.0.0.1:8080/healthz", 5),
        ("http://127.0.0.1:8080/api/healthz", 5),
    ]


def test_existing_dashboard_container_skips_port_check(monkeypatch, project):
    calls = []

    def response(argv):
        if argv[:3] == ["docker", "container", "inspect"]:
            return completed(
                argv,
                stdout=json.dumps(
                    [{"NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}}}]
                ),
            )
        if argv[-2:] == ["ps", "-q"]:
            return completed(argv, stdout="existing\n")
        return completed(argv)

    install_docker(monkeypatch, calls, response)
    monkeypatch.setattr(
        dashboard_stack,
        "_find_web_port",
        lambda *a, **k: pytest.fail("the existing dashboard owns port 8080, should not search for a new one"),
    )
    monkeypatch.setattr(
        dashboard_stack.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: type("Response", (), {"status": 200, "close": lambda self: None})(),
    )

    result = dashboard_stack.run_dashboard()

    assert result.ok
    assert result.url == "http://127.0.0.1:8080"
