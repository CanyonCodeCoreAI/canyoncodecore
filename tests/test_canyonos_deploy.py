import os

import pytest

from canyonos import deploy as deploy_cmd
from canyonos.gc import GCError

CONFIG_PATH = os.path.join("config", "global_controller.yaml")
STATE = {"container_id": "abc", "port": 8000}


@pytest.fixture
def deployable(monkeypatch, tmp_path):
    """Every step run_deploy drives succeeds unless overridden, in a project whose config exists."""
    (tmp_path / "config").mkdir()
    (tmp_path / CONFIG_PATH).write_text("agents: []\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(deploy_cmd, "workspace_relative", lambda p: p)
    monkeypatch.setattr(deploy_cmd, "run_init", lambda banner=True, extra_env=None: None)
    monkeypatch.setattr(deploy_cmd, "run_sync", lambda: True)
    monkeypatch.setattr(deploy_cmd, "load_state", lambda: dict(STATE))
    monkeypatch.setattr(deploy_cmd, "workflow_api_port", lambda _config: 8080)
    monkeypatch.setattr(deploy_cmd, "port_in_use", lambda _port: False)
    monkeypatch.setattr(deploy_cmd, "post_deploy", lambda *_a: None)


def test_a_config_path_outside_the_project_raises(monkeypatch, deployable):
    monkeypatch.setattr(deploy_cmd, "workspace_relative", lambda _p: None)

    with pytest.raises(RuntimeError, match="Config must be inside the project directory"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)


def test_a_half_built_car_project_fails_before_tearing_anything_down(
    monkeypatch, tmp_path, deployable, capsys
):
    """An interrupted `canyonos build` leaves a .car directory with no config in it.
    The container would resolve the .car path, so the CLI has to name that one."""
    reached = []
    monkeypatch.setattr(deploy_cmd, "run_init", lambda **_kwargs: reached.append("init"))
    (tmp_path / ".car").mkdir()

    assert deploy_cmd.run_deploy(quiet=True) is None

    printed = " ".join(capsys.readouterr().out.split())
    assert "Config file not found" in printed
    assert os.path.join(".car", "config", "global_controller.yaml") in printed
    assert reached == []


def test_a_sync_failure_raises(monkeypatch, deployable):
    monkeypatch.setattr(deploy_cmd, "run_sync", lambda: False)

    with pytest.raises(RuntimeError, match="Could not sync the project"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)


def test_an_occupied_api_port_raises_before_post_deploy(monkeypatch, deployable):
    """Checked after run_init() (which already tore down any previous deploy), so a still-live
    prior run doesn't read as an unrelated conflict -- only a genuinely occupied port does."""
    calls = []
    monkeypatch.setattr(deploy_cmd, "port_in_use", lambda _port: True)
    monkeypatch.setattr(deploy_cmd, "post_deploy", lambda *a: calls.append(a))

    with pytest.raises(RuntimeError, match="Port 8080 is already in use"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)

    assert calls == []


def test_a_post_deploy_failure_is_reraised_as_a_runtime_error(monkeypatch, deployable):
    def boom(*_a):
        raise GCError("Deploy failed: conflict")

    monkeypatch.setattr(deploy_cmd, "post_deploy", boom)

    with pytest.raises(RuntimeError, match="Deploy failed: conflict"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)


def test_quiet_returns_state_without_streaming(monkeypatch, deployable):
    def unexpected(*_a, **_k):
        raise AssertionError("quiet=True should skip the log-tail/dashboard UI")

    monkeypatch.setattr(deploy_cmd, "_stream_logs_and_autoserve", unexpected)

    assert deploy_cmd.run_deploy(CONFIG_PATH, quiet=True) == STATE


def test_non_quiet_still_streams_and_returns_state(monkeypatch, deployable):
    calls = []
    monkeypatch.setattr(
        deploy_cmd, "_stream_logs_and_autoserve",
        lambda state, api_port, config_path, serve, verbose: calls.append(
            (state, api_port, config_path, serve, verbose)
        ),
    )

    assert deploy_cmd.run_deploy(CONFIG_PATH, serve=False, verbose=True) == STATE
    assert calls == [(STATE, 8080, CONFIG_PATH, False, True)]


def test_extra_env_and_banner_are_forwarded_to_run_init(monkeypatch, deployable):
    seen = {}
    monkeypatch.setattr(
        deploy_cmd, "run_init",
        lambda banner=True, extra_env=None: seen.update(banner=banner, extra_env=extra_env),
    )

    deploy_cmd.run_deploy(CONFIG_PATH, quiet=True, extra_env={"CANYONOS_LLM_STUB_TEXT": "test"}, banner=False)

    assert seen == {"banner": False, "extra_env": {"CANYONOS_LLM_STUB_TEXT": "test"}}
