import json
import subprocess

import pytest

from canyonos import init as init_cmd


def test_docker_runtime_start_uses_bounded_translated_command(monkeypatch):
    calls = []
    monkeypatch.setattr(init_cmd, "docker_running", lambda: False)
    monkeypatch.setattr(init_cmd, "docker_start_command", lambda: ["orb", "start"])

    def failed(argv, **kwargs):
        calls.append((argv, kwargs))
        raise RuntimeError("Starting the Docker runtime failed: launch failed")

    monkeypatch.setattr(init_cmd, "run_docker", failed)

    with pytest.raises(RuntimeError, match="Starting the Docker runtime failed: launch failed"):
        init_cmd.ensure_docker_running()

    assert calls == [
        (
            ["orb", "start"],
            {
                "timeout": init_cmd.DOCKER_START_TIMEOUT,
                "action": "Starting the Docker runtime",
                "check": True,
            },
        )
    ]


def test_failed_state_write_preserves_the_previous_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    original = {"container_id": "old", "port": 8000}
    state_path.write_text(json.dumps(original))
    monkeypatch.setattr(init_cmd, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(init_cmd, "STATE_PATH", str(state_path))
    monkeypatch.setattr(
        init_cmd.json,
        "dump",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        init_cmd.save_state("new", 8001)

    assert json.loads(state_path.read_text()) == original
    assert list(tmp_path.glob("state.*.tmp")) == []


def test_state_save_failure_removes_the_new_container(monkeypatch):
    calls = []
    monkeypatch.setattr(init_cmd, "ensure_docker_running", lambda: None)
    monkeypatch.setattr(init_cmd, "quit_existing", lambda: None)
    monkeypatch.setattr(init_cmd, "pull_image", lambda: None)
    monkeypatch.setattr(
        init_cmd, "run_container", lambda extra_env=None: ("abcdef123456", 8000)
    )
    monkeypatch.setattr(
        init_cmd, "save_state", lambda *_a: (_ for _ in ()).throw(OSError("disk full"))
    )
    monkeypatch.setattr(
        init_cmd.subprocess,
        "run",
        lambda argv, **_k: (
            calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )

    with pytest.raises(RuntimeError, match="disk full"):
        init_cmd.run_init(banner=False)

    assert calls == [["docker", "rm", "-f", "abcdef123456"]]


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "not json",
        "[]",
        '{"container_id": "abc"}',
        '{"container_id": "abc", "port": 70000}',
    ],
)
def test_invalid_state_has_recovery_guidance(monkeypatch, tmp_path, contents):
    state_path = tmp_path / "state.json"
    state_path.write_text(contents)
    monkeypatch.setattr(init_cmd, "STATE_PATH", str(state_path))

    with pytest.raises(RuntimeError, match="Move or remove that file"):
        init_cmd.load_state()
