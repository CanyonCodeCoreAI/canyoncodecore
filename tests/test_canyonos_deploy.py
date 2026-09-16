import pytest

from canyonos import deploy as deploy_cmd
from canyonos.gc import GCError

CONFIG_PATH = "config/global_controller.yaml"
STATE = {"container_id": "abc", "port": 8000}


@pytest.fixture
def deployable(monkeypatch):
    """Every step run_deploy drives succeeds unless overridden."""
    monkeypatch.setattr(deploy_cmd, "workspace_relative", lambda p: p)
    monkeypatch.setattr(deploy_cmd, "run_init", lambda banner=True, extra_env=None: None)
    monkeypatch.setattr(deploy_cmd, "run_sync", lambda: True)
    monkeypatch.setattr(deploy_cmd, "load_state", lambda: dict(STATE))
    monkeypatch.setattr(deploy_cmd, "workflow_api_port", lambda _config: 8080)
    monkeypatch.setattr(deploy_cmd, "port_in_use", lambda _port: False)
    monkeypatch.setattr(deploy_cmd, "post_deploy", lambda *_a: None)
    monkeypatch.setattr(deploy_cmd, "_start_dashboard", lambda: None)
    monkeypatch.setattr(deploy_cmd, "run_quit", lambda: None)


def test_a_config_path_outside_the_project_raises(monkeypatch, deployable):
    monkeypatch.setattr(deploy_cmd, "workspace_relative", lambda _p: None)

    with pytest.raises(RuntimeError, match="Config must be inside the project directory"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)


def test_a_sync_failure_raises(monkeypatch, deployable):
    cleaned = []
    monkeypatch.setattr(deploy_cmd, "run_sync", lambda: False)
    monkeypatch.setattr(deploy_cmd, "run_quit", lambda: cleaned.append(True))

    with pytest.raises(RuntimeError, match="Could not sync the project"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)

    assert cleaned == [True]


def test_ctrl_c_during_setup_cleans_up_the_partial_deploy(monkeypatch, deployable):
    cleaned = []
    monkeypatch.setattr(
        deploy_cmd,
        "run_sync",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(deploy_cmd, "run_quit", lambda: cleaned.append(True))

    with pytest.raises(KeyboardInterrupt):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)

    assert cleaned == [True]


def test_an_occupied_api_port_raises_before_post_deploy(monkeypatch, deployable):
    """Checked after run_init() (which already tore down any previous deploy), so a still-live
    prior run doesn't read as an unrelated conflict -- only a genuinely occupied port does."""
    calls = []
    monkeypatch.setattr(deploy_cmd, "port_in_use", lambda _port: True)
    monkeypatch.setattr(deploy_cmd, "post_deploy", lambda *a: calls.append(a))

    with pytest.raises(RuntimeError, match="Port 8080 is already in use"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)

    assert calls == []


def test_a_log_monitor_failure_cleans_up_the_partial_deploy(monkeypatch, deployable):
    cleaned = []
    monkeypatch.setattr(
        deploy_cmd,
        "_stream_logs_and_autoserve",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("logs broke")),
    )
    monkeypatch.setattr(deploy_cmd, "run_quit", lambda: cleaned.append(True))

    with pytest.raises(RuntimeError, match="logs broke"):
        deploy_cmd.run_deploy(CONFIG_PATH, serve=False)

    assert cleaned == [True]


def test_a_post_deploy_failure_is_reraised_as_a_runtime_error(monkeypatch, deployable):
    def boom(*_a):
        raise GCError("Deploy failed: conflict")

    monkeypatch.setattr(deploy_cmd, "post_deploy", boom)

    with pytest.raises(RuntimeError, match="Deploy failed: conflict"):
        deploy_cmd.run_deploy(CONFIG_PATH, quiet=True)


def test_a_failure_after_the_workflow_is_up_leaves_the_deploy_alone(monkeypatch, deployable):
    """Summary rendering runs after the workflow is serving, so anything that
    fails there is a reporting problem -- not a reason to tear the stack down."""
    cleaned = []

    def ready_then_fail(*_a, on_ready, **_k):
        on_ready(True)
        raise RuntimeError("could not resolve endpoints")

    monkeypatch.setattr(deploy_cmd, "_stream_logs_and_autoserve", ready_then_fail)
    monkeypatch.setattr(deploy_cmd, "run_quit", lambda: cleaned.append(True))

    with pytest.raises(RuntimeError, match="could not resolve endpoints"):
        deploy_cmd.run_deploy(CONFIG_PATH, serve=False)

    assert cleaned == []


def test_quiet_returns_state_without_streaming(monkeypatch, deployable):
    def unexpected(*_a, **_k):
        raise AssertionError("quiet=True should skip the log-tail/dashboard UI")

    monkeypatch.setattr(deploy_cmd, "_stream_logs_and_autoserve", unexpected)

    assert deploy_cmd.run_deploy(CONFIG_PATH, quiet=True) == STATE


def test_non_quiet_still_streams_and_returns_state(monkeypatch, deployable):
    calls = []
    monkeypatch.setattr(
        deploy_cmd, "_stream_logs_and_autoserve",
        lambda state, api_port, config_path, serve, verbose, on_ready: calls.append(
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
