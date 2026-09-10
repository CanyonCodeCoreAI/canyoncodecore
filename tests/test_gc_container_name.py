import subprocess

import pytest

from canyonos import init as init_cmd

CONTAINER_ID = "a" * 64
LEFTOVER_ID = "b" * 64


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeDocker:
    """Stands in for subprocess.run: records argv and scripts `docker run`."""

    def __init__(self):
        self.calls = []
        self.named_id = None
        self.runs = [completed(["docker", "run"], stdout=f"{CONTAINER_ID}\n")]

    def __call__(self, argv, **_):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            if self.named_id is None:
                return completed(argv, returncode=1, stderr="No such object")
            return completed(argv, stdout=f"{self.named_id}\n")
        if argv[:2] == ["docker", "run"]:
            result = self.runs.pop(0)
            if result.returncode != 0:
                # Docker creates the container before it publishes ports, so a
                # rejected binding still leaves the name taken.
                self.named_id = LEFTOVER_ID
            return result
        if argv[:3] == ["docker", "rm", "-f"]:
            self.named_id = None
        return completed(argv)

    @property
    def run_calls(self):
        return [argv for argv in self.calls if argv[:2] == ["docker", "run"]]

    @property
    def removals(self):
        return [argv for argv in self.calls if argv[:3] == ["docker", "rm", "-f"]]

    def order(self):
        return [tuple(argv[:3]) for argv in self.calls]


def no_state():
    raise FileNotFoundError


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(init_cmd.subprocess, "run", fake)
    monkeypatch.setattr(init_cmd, "_port_reachable", lambda _port: True)
    monkeypatch.setattr(init_cmd, "load_state", no_state)
    return fake


def name_flag(argv):
    return argv[argv.index("--name") + 1]


def test_the_container_is_started_under_a_fixed_name(docker):
    container_id, port = init_cmd.run_container()

    assert container_id == CONTAINER_ID
    assert port == init_cmd.GC_CONTAINER_PORT
    assert name_flag(docker.run_calls[0]) == init_cmd.GC_CONTAINER_NAME


def test_a_leftover_container_holding_the_name_is_removed_first(docker):
    docker.named_id = LEFTOVER_ID

    init_cmd.run_container()

    inspects = [argv for argv in docker.calls if argv[:2] == ["docker", "inspect"]]
    assert inspects[0][-1] == init_cmd.GC_CONTAINER_NAME
    assert docker.removals == [["docker", "rm", "-f", LEFTOVER_ID]]
    assert docker.order().index(("docker", "rm", "-f")) < docker.order().index(("docker", "run", "-d"))


def test_the_container_recorded_in_state_is_left_alone(docker, monkeypatch):
    """quit_existing() owns the recorded controller; run_container only clears strays."""
    docker.named_id = CONTAINER_ID
    monkeypatch.setattr(init_cmd, "load_state", lambda: {"container_id": CONTAINER_ID, "port": 8000})

    init_cmd.run_container()

    assert docker.removals == []


def test_a_port_collision_retry_still_gets_the_name(docker):
    docker.runs = [
        completed(["docker", "run"], returncode=125, stderr="Bind for 127.0.0.1:8000 failed: port is already allocated"),
        completed(["docker", "run"], stdout=f"{CONTAINER_ID}\n"),
    ]

    container_id, port = init_cmd.run_container()

    assert (container_id, port) == (CONTAINER_ID, init_cmd.GC_CONTAINER_PORT + 1)
    assert [name_flag(argv) for argv in docker.run_calls] == [init_cmd.GC_CONTAINER_NAME] * 2
    assert docker.removals == [["docker", "rm", "-f", LEFTOVER_ID]]
