import subprocess

import pytest

from canyonos import init as init_cmd

CONTAINER_ID = "a" * 64
LEFTOVER_ID = "b" * 64


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def name_flag(argv):
    return argv[argv.index("--name") + 1]


class FakeDocker:
    """Stands in for subprocess.run: records argv and scripts `docker run`."""

    def __init__(self):
        self.calls = []
        self.named_id = None
        self.named_running = False
        self.runs = [completed(["docker", "run"], stdout=f"{CONTAINER_ID}\n")]

    def __call__(self, argv, **_):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            if self.named_id is None:
                return completed(argv, returncode=1, stderr="No such object")
            running = "true" if self.named_running else "false"
            return completed(argv, stdout=f"{self.named_id} {running}\n")
        if argv[:2] == ["docker", "run"]:
            if self.named_id is not None:
                return completed(
                    argv,
                    returncode=125,
                    stderr=(
                        "docker: Error response from daemon: Conflict. The container "
                        f'name "/{name_flag(argv)}" is already in use by container '
                        f'"{self.named_id}".'
                    ),
                )
            result = self.runs.pop(0)
            # Docker creates the container before it publishes ports, so even a
            # run rejected over a port binding leaves the name taken.
            self.named_id = CONTAINER_ID if result.returncode == 0 else LEFTOVER_ID
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


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(init_cmd.subprocess, "run", fake)
    monkeypatch.setattr(init_cmd, "_port_reachable", lambda _port: True)
    return fake


def test_the_container_is_started_under_a_fixed_name(docker):
    container_id, port, _socket = init_cmd.run_container()

    assert container_id == CONTAINER_ID
    assert port == init_cmd.GC_CONTAINER_PORT
    assert name_flag(docker.run_calls[0]) == init_cmd.GC_CONTAINER_NAME


def test_a_leftover_container_holding_the_name_is_removed_first(docker):
    docker.named_id = LEFTOVER_ID

    container_id, _port, _socket = init_cmd.run_container()

    # The fake rejects `docker run` while the name is held, exactly as docker
    # does, so coming back with a container at all proves the order.
    assert container_id == CONTAINER_ID
    assert docker.removals == [["docker", "rm", "-f", LEFTOVER_ID]]


def test_a_running_container_holding_the_name_is_refused_not_removed(docker):
    """Someone's live controller -- removing it would orphan its Redis and agents."""
    docker.named_id = LEFTOVER_ID
    docker.named_running = True

    with pytest.raises(RuntimeError, match="canyonos quit"):
        init_cmd.run_container()

    assert docker.removals == []
    assert docker.run_calls == []


def test_a_port_collision_retry_still_gets_the_name(docker):
    docker.runs = [
        completed(["docker", "run"], returncode=125, stderr="Bind for 127.0.0.1:8000 failed: port is already allocated"),
        completed(["docker", "run"], stdout=f"{CONTAINER_ID}\n"),
    ]

    container_id, port, _socket = init_cmd.run_container()

    assert (container_id, port) == (CONTAINER_ID, init_cmd.GC_CONTAINER_PORT + 1)
    assert [name_flag(argv) for argv in docker.run_calls] == [init_cmd.GC_CONTAINER_NAME] * 2
    assert docker.removals == [["docker", "rm", "-f", LEFTOVER_ID]]
