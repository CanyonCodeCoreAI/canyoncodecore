from canyonos import stop as stop_cmd


def test_dashboard_stop_failure_is_not_reported_as_success(monkeypatch):
    reported = []
    monkeypatch.setattr(
        stop_cmd,
        "require_state",
        lambda: {"container_id": "abcdef123456", "port": 8000},
    )
    monkeypatch.setattr(stop_cmd, "post_clean", lambda _port: None)
    monkeypatch.setattr(stop_cmd, "stop_dashboard", lambda: False)
    monkeypatch.setattr(stop_cmd.ui, "ok", lambda _message: reported.append("ok"))
    monkeypatch.setattr(stop_cmd.ui, "warn", lambda message: reported.append(str(message)))

    stop_cmd.run_stop()

    assert reported == ["Deploy stopped, but the dashboard did not. Run `canyonos quit` to remove it."]
