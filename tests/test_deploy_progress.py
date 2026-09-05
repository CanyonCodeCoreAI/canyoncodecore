import pytest

from canyonos import deploy as deploy_cmd
from canyonos.deploy import PhaseTracker


def drive(lines):
    """Feed lines to a tracker, returning (spinners, completions, errored)."""
    tracker = PhaseTracker()
    spinners, done = [], []
    errored = False
    for line in lines:
        message, completed, is_error = tracker.feed(line)
        if is_error:
            errored = True
        if message:
            spinners.append(message)
        if completed:
            done.append(completed)
    return tracker, spinners, done, errored


def test_a_full_run_reports_each_phase_once():
    _, spinners, done, errored = drive(
        [
            "INFO:ventis:Generating stub: a.yaml -> a_stub.py\n",
            "INFO:ventis:Compiling gRPC proto: a.proto\n",
            "INFO:ventis:Building 3 Docker image(s) via `docker buildx bake`.\n",
            "#5 [4/7] RUN pip install -r requirements.txt\n",
            "INFO:ventis:Build complete.\n",
            "INFO:ventis:Deploying from config: config.yaml\n",
            "INFO:ventis.controller.global_controller:Redis launched on 1 node(s).\n",
            "INFO:ventis.controller.global_controller:Waiting for 2 replica(s) to become healthy (timeout=300s)...\n",
            "INFO:ventis.controller.global_controller:Controller Intent (127.0.0.1:50051) is ready.\n",
            "INFO:ventis.controller.global_controller:Controller Metrics (127.0.0.1:50052) is ready.\n",
        ]
    )
    assert not errored
    assert done == ["Build complete", "Redis ready"]
    assert "Building 3 images..." in spinners
    assert spinners[-1] == "Starting agents (2/2 ready)..."


def test_phases_are_matched_in_the_order_the_container_emits_them():
    """Redis and stale-container cleanup are logged by GlobalController.__init__,
    which runs before `Deploying from config:` -- so the matcher must not assume
    the config line comes first.
    """
    _, spinners, done, _ = drive(
        [
            "INFO:ventis.controller.global_controller:Checking for stale containers from previous runs...\n",
            "INFO:ventis.controller.global_controller:Redis launched on 1 node(s).\n",
            "INFO:ventis:Deploying from config: config.yaml\n",
        ]
    )
    assert done == ["Redis ready"]
    assert spinners == ["Cleaning up stale containers...", "Starting deploy..."]


def test_repeated_build_lines_collapse_to_one_spinner_update():
    _, spinners, _, _ = drive(
        [
            "INFO:ventis:Generating stub: a.yaml -> a_stub.py\n",
            "INFO:ventis:Generating stub: b.yaml -> b_stub.py\n",
            "INFO:ventis:Generating Docker context for 'b'\n",
        ]
    )
    assert spinners == ["Generating stubs and Docker contexts..."]


def test_a_run_with_nothing_to_build_still_reports_the_phase():
    _, _, done, _ = drive(
        [
            "INFO:ventis:No Docker images to build.\n",
            "INFO:ventis:Build complete.\n",
        ]
    )
    assert done == ["No images to build", "Build complete"]


def test_agent_progress_counts_up_against_the_announced_total():
    tracker, spinners, _, _ = drive(
        [
            "INFO:ventis.controller.global_controller:Waiting for 3 replica(s) to become healthy (timeout=300s)...\n",
            "INFO:ventis.controller.global_controller:Controller A (127.0.0.1:1) is ready.\n",
            "INFO:ventis.controller.global_controller:Controller B (127.0.0.1:2) is ready.\n",
        ]
    )
    assert spinners[-1] == "Starting agents (2/3 ready)..."
    assert tracker.replicas_total == 3


def test_replicas_of_one_agent_are_counted_separately():
    """`Controller %s is ready.` logs the agent name, which repeats across that
    agent's replicas -- the endpoint is what distinguishes them.
    """
    tracker, spinners, _, _ = drive(
        [
            "INFO:ventis.controller.global_controller:Waiting for 2 replica(s) to become healthy (timeout=300s)...\n",
            "INFO:ventis.controller.global_controller:Controller Echo (127.0.0.1:50051) is ready.\n",
            "INFO:ventis.controller.global_controller:Controller Echo (127.0.0.1:50052) is ready.\n",
        ]
    )
    assert spinners[-1] == "Starting agents (2/2 ready)..."
    assert tracker.agents_ready_message() == ("2 agent(s) ready", True)


def test_a_re_read_ready_line_does_not_double_count():
    tracker, _, _, _ = drive(
        [
            "INFO:ventis.controller.global_controller:Waiting for 2 replica(s) to become healthy (timeout=300s)...\n",
            "INFO:ventis.controller.global_controller:Controller Echo (127.0.0.1:50051) is ready.\n",
            "INFO:ventis.controller.global_controller:Controller Echo (127.0.0.1:50051) is ready.\n",
        ]
    )
    assert tracker.agents_ready_message() == (
        "Workflow up, but only 1/2 agents reported healthy",
        False,
    )


def test_coming_up_short_of_the_announced_replicas_is_not_reported_as_success():
    """`_wait_for_healthy` gives up after its timeout and the controller starts
    anyway, so the up-marker can arrive with agents still unhealthy.
    """
    tracker, _, _, _ = drive(
        [
            "INFO:ventis.controller.global_controller:Waiting for 3 replica(s) to become healthy (timeout=300s)...\n",
            "INFO:ventis.controller.global_controller:Controller A (127.0.0.1:1) is ready.\n",
        ]
    )
    message, all_ready = tracker.agents_ready_message()
    assert not all_ready
    assert message == "Workflow up, but only 1/3 agents reported healthy"


def test_a_run_that_never_announced_replicas_still_reports_ready():
    tracker, _, _, _ = drive(["INFO:ventis:Build complete.\n"])
    assert tracker.agents_ready_message() == ("Workflow ready", True)


def test_replicas_ready_without_an_announced_total_still_reports_progress():
    _, spinners, _, _ = drive(
        ["INFO:ventis.controller.global_controller:Controller A (127.0.0.1:1) is ready.\n"]
    )
    assert spinners == ["Starting agents..."]


@pytest.mark.parametrize(
    "line",
    [
        "ERROR:ventis:Config file not found: missing.yaml\n",
        "Traceback (most recent call last):\n",
        "ERROR: failed to solve: process \"/bin/sh -c pip install\" did not complete successfully\n",
    ],
)
def test_fatal_lines_are_flagged(line):
    _, _, _, errored = drive([line])
    assert errored


@pytest.mark.parametrize(
    "line",
    [
        "WARNING:ventis.controller.global_controller:otel.destinations not configured -- no OTel metrics collection will happen.\n",
        "  Warning: no entrypoint mapping for 'agent'\n",
    ],
)
def test_benign_warnings_do_not_trip_the_error_path(line):
    _, _, _, errored = drive([line])
    assert not errored


def test_the_deploy_is_only_declared_dead_after_two_consecutive_checks(monkeypatch):
    """One dropped request shouldn't end a deploy that is merely busy."""
    replies = iter([None, {"running": True}, None, None])
    monkeypatch.setattr(deploy_cmd, "deploy_status", lambda _port: next(replies))
    state = {"port": 1}

    misses = 0
    verdicts = []
    for _ in range(4):
        dead, misses = deploy_cmd._deploy_is_dead(state, misses)
        verdicts.append(dead)

    # a miss, then a recovery that resets the count, then two misses in a row
    assert verdicts == [False, False, False, True]


def test_a_running_deploy_is_never_declared_dead(monkeypatch):
    monkeypatch.setattr(deploy_cmd, "deploy_status", lambda _port: {"running": True})
    dead, misses = deploy_cmd._deploy_is_dead({"port": 1}, 1)
    assert not dead and misses == 0


def test_the_clis_own_status_requests_are_not_shown_or_buffered(monkeypatch):
    """The container logs every request the CLI makes to it, so its own polling
    lands in the stream it is reading.
    """
    shown = []
    monkeypatch.setattr(deploy_cmd.ui, "ok", lambda m: shown.append(m))
    monkeypatch.setattr(deploy_cmd.ui, "warn", lambda m: shown.append(m))
    monkeypatch.setattr(deploy_cmd, "_deploy_summary", lambda *a: ("url", []))

    lines = deploy_cmd._queued_lines(
        iter(
            [
                '172.17.0.1 - - [04/Sep/2026 21:00:00] "GET /status HTTP/1.1" 200 -\n',
                "INFO:ventis:Build complete.\n",
                "INFO:ventis.controller.global_controller:Global controller started, polling every 5s...\n",
            ]
        )
    )
    summary = deploy_cmd._tail_quiet(lines, {"port": 1}, 8080, serve=False)

    assert summary == ("url", [])
    assert shown == ["Build complete", "Workflow ready"]


def test_a_build_that_dies_silently_does_not_hang(monkeypatch, capsys):
    """A failed build leaves `docker logs -f` open with nothing more to say, so
    the wait has to end on /status rather than on the stream closing.
    """
    monkeypatch.setattr(deploy_cmd, "_STATUS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(deploy_cmd, "_REVEAL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(deploy_cmd, "deploy_status", lambda _p: {"running": False})

    lines = deploy_cmd._queued_lines(iter(["INFO:ventis:Building 2 Docker image(s) via `x`.\n"]))
    # The queue never yields None: the stream stays open, as it does in reality.
    lines.put = lambda *a, **k: None

    summary = deploy_cmd._tail_quiet(lines, {"port": 1}, 8080, serve=False)

    assert summary is None
    assert "Building 2 Docker image(s)" in capsys.readouterr().out
