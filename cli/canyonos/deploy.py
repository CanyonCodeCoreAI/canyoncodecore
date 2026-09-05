"""
Logic for `canyonos deploy`: copy the project into the container's /workspace
volume (via `canyonos sync`), then tell the Global Controller container to
build and deploy it. The container's `canyonos deploy` handles both the build
(stubs, protos, Docker images) and the launch -- the CLI just ships files,
triggers it, and watches the logs.

That log stream is mostly noise the user didn't ask for (a whole `docker buildx
bake` transcript, among other things), so by default only the phase transitions
worth seeing are rendered and everything else is dropped. `-v` streams it all,
and a failure reveals the output it had been hiding.

Once the deploy's logs report the workflow is actually up, `canyonos serve`
is kicked off automatically so the local dashboard is ready without an extra
manual step.
"""

import queue
import re
import subprocess
import threading
import time
from collections import deque

from rich.panel import Panel
from rich.text import Text

from canyonos import ui
from canyonos.constants import (
    WORKFLOW_ROUTE,
    default_config_path,
    workflow_api_port,
    workspace_relative,
)
from canyonos.gc import GCError, deploy_status, post_deploy, workflow_endpoints
from canyonos.theme import GREEN, WHITE
from canyonos.init import load_state, run_init
from canyonos.serve import serve_dashboard
from canyonos.sync import run_sync

LOCAL_HOSTS = ("127.0.0.1", "localhost")

# Logged exactly once by GlobalController.run(), right after `_wait_for_healthy()`
# returns -- the signal that the workflow finished coming up and entered its
# steady-state polling loop.
_WORKFLOW_UP_MARKER = "Global controller started, polling every"

# Substrings that mean the in-container deploy hit something fatal. `WARNING:` is
# deliberately absent: the OTel-not-configured notice and stub_generator's
# "Warning:" lines are benign and fire on nearly every run.
_ERROR_MARKERS = (
    "ERROR:",
    "Traceback (most recent call last):",
    "ERROR: failed to solve",
    "process did not complete successfully",
)

# (substring, spinner message, completed message). A None spinner message keeps
# whatever the spinner already shows; a None completed message prints nothing.
# Matched by substring against the raw line, so a phase that never runs is simply
# never matched -- nothing here assumes a phase happens, or happens in order.
_PHASES = (
    ("Generating stub:", "Generating stubs and Docker contexts...", None),
    ("Compiling gRPC proto:", "Generating stubs and Docker contexts...", None),
    ("Generating Docker context", "Generating stubs and Docker contexts...", None),
    ("Building Docker image:", "Building images...", None),
    ("No Docker images to build.", None, "No images to build"),
    ("Build complete.", None, "Build complete"),
    ("Deploying from config:", "Starting deploy...", None),
    ("Checking for stale containers", "Cleaning up stale containers...", None),
    ("Redis launched on", None, "Redis ready"),
    ("Docker container(s) across", "Starting agents...", None),
)

_IMAGE_COUNT = re.compile(r"Building (\d+) Docker image\(s\) via")
_REPLICA_COUNT = re.compile(r"Waiting for (\d+) replica\(s\) to become healthy")
# The name repeats across replicas of one agent, so the endpoint is what makes a
# ready line unique.
_READY = re.compile(r"Controller (\S+ \([^)]+\)) is ready\.")

# Enough to hold a buildx failure block plus a Python traceback; 40 (what
# `canyonos test` tails) truncates both.
_RECENT_LINES = 200

# The container logs every request the CLI makes to it, so its own polling shows
# up in the stream it is reading.
_OWN_REQUEST_MARKER = "GET /status HTTP/1.1"

_STATUS_POLL_SECONDS = 2.0

# Upper bound on how long to keep collecting output after a failure is spotted.
_REVEAL_GRACE_SECONDS = 30.0


class PhaseTracker:
    """Turns the container's log lines into the handful of events worth showing.

    `feed()` returns (spinner_message, completed_message, is_error) -- any of
    which may be None -- so the caller owns all printing.
    """

    def __init__(self):
        self.spinner = None
        self.replicas_total = 0
        self.replicas_ready = set()

    def _agent_progress(self):
        if self.replicas_total:
            return f"Starting agents ({len(self.replicas_ready)}/{self.replicas_total} ready)..."
        return "Starting agents..."

    def feed(self, line):
        if any(marker in line for marker in _ERROR_MARKERS):
            return None, None, True

        count = _IMAGE_COUNT.search(line)
        if count:
            self.spinner = f"Building {count.group(1)} images..."
            return self.spinner, None, False

        replicas = _REPLICA_COUNT.search(line)
        if replicas:
            self.replicas_total = int(replicas.group(1))
            self.spinner = self._agent_progress()
            return self.spinner, None, False

        ready = _READY.search(line)
        if ready:
            self.replicas_ready.add(ready.group(1))
            self.spinner = self._agent_progress()
            return self.spinner, None, False

        for marker, spinner, done in _PHASES:
            if marker in line:
                # Repeats (one `Generating stub:` per agent) collapse: the
                # spinner is only re-emitted when the message actually changes.
                if spinner and spinner != self.spinner:
                    self.spinner = spinner
                    return spinner, done, False
                return None, done, False

        return None, None, False

    def agents_ready_message(self):
        """(message, all_ready). `_wait_for_healthy` gives up after its timeout and
        lets the controller start anyway, so the workflow can come up short.
        """
        ready = len(self.replicas_ready)
        if not self.replicas_total:
            return "Workflow ready", True
        if ready < self.replicas_total:
            return f"Workflow up, but only {ready}/{self.replicas_total} agents reported healthy", False
        return f"{ready} agent(s) ready", True


def run_deploy(config_path=None, serve=True, verbose=False):
    # Left as None when unset: canyonos resolves the artifact layout itself.
    if config_path is not None:
        config_path = workspace_relative(config_path)
        if config_path is None:
            ui.fail("Config must be inside the project directory being synced.")
            return

    run_init()

    # Copy the current project into the container before building/deploying.
    if not run_sync():
        return

    state = load_state()

    # Read for display only -- canyonos resolves the path it actually deploys.
    api_port = workflow_api_port(config_path or default_config_path())

    try:
        post_deploy(state["port"], config_path)
        _stream_logs_and_autoserve(state, api_port, serve=serve, verbose=verbose)
    except GCError as e:
        ui.fail(e)


def workflow_targets(gc_port, api_port):
    """(name, host, port) for each deployed workflow.

    The container reports the address it actually placed each workflow at, so a
    workflow running on another machine shows that machine's public IP. The
    local port mapping is the fallback when it reports nothing.
    """
    targets = [
        (
            endpoint.get("name"),
            "127.0.0.1" if endpoint["host"] in LOCAL_HOSTS else endpoint["host"],
            endpoint["port"],
        )
        for endpoint in workflow_endpoints(gc_port)
        if endpoint.get("host") and endpoint.get("port")
    ]
    if targets:
        return targets
    return [(None, "127.0.0.1", api_port)] if api_port else []


def _summary_body(dashboard_url, targets):
    body = Text()
    body.append("Dashboard  ", "dim")
    if dashboard_url:
        body.append(dashboard_url, f"bold {GREEN}")
    else:
        body.append("not running -- start it with `canyonos serve`", WHITE)

    for name, host, port in targets:
        base = f"http://{host}:{port}"
        body.append("\n")
        if name:
            body.append(f"\n{name}", f"bold {WHITE}")
        body.append("\nPOST       ", "dim")
        body.append(f"{base}/{WORKFLOW_ROUTE}", f"bold {GREEN}")
        body.append("\nbody       ", "dim")
        body.append('{"query": "your question here"}', WHITE)
        body.append("\npoll       ", "dim")
        body.append(f"{base}/status/<request_id>", WHITE)
        if host not in LOCAL_HOSTS:
            body.append(f"\n           needs inbound TCP {port} open on {host}", "dim")
    return body


def print_deploy_summary(dashboard_url, targets):
    """The one screen printed once everything is up: dashboard and workflow endpoints.

    Under `-v` it is printed again on exit, because the log tail continues
    afterwards and would otherwise scroll it out of sight. Quiet mode prints
    nothing after it, so once is enough.
    """
    ui.blank()
    ui.panel(
        Panel(
            _summary_body(dashboard_url, targets),
            title=f"[bold {GREEN}]Deploy is live[/]",
            title_align="left",
            border_style=GREEN,
            padding=(1, 4),
        )
    )
    ui.blank()


def _start_dashboard():
    """The dashboard's URL, or None -- a dashboard that won't start doesn't fail the deploy."""
    try:
        return serve_dashboard().url
    except Exception as e:
        ui.fail(f"Could not start the dashboard automatically: {e}")
        ui.hint("Run `canyonos serve` manually to view it.")
        return None


def _deploy_summary(state, api_port, serve):
    summary = (
        _start_dashboard() if serve else None,
        workflow_targets(state["port"], api_port),
    )
    print_deploy_summary(*summary)
    return summary


def _interrupted(summary=None):
    ui.blank()
    ui.say("Stopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
    ui.hint("To resubscribe to log stream run `canyonos logs`.")
    if summary is not None:
        print_deploy_summary(*summary)


def _tail_verbose(stream, state, api_port, serve):
    """Every log line, verbatim -- what `-v` restores.

    Ctrl+C reprints the summary here but not in quiet mode: only this tail keeps
    printing past it, so only here has it scrolled out of sight.
    """
    summary = None
    try:
        for line in stream:
            print(line, end="")
            if summary is None and _WORKFLOW_UP_MARKER in line:
                summary = _deploy_summary(state, api_port, serve)
    except KeyboardInterrupt:
        _interrupted(summary)


def _tail_quiet(lines, state, api_port, serve):
    """Only the phase transitions, until the workflow is up or something fails.

    Nothing is echoed raw: the buildx transcript, canyonos' bare prints and grpc's
    stderr have no common prefix to filter on, so anything unrecognized is
    dropped rather than allow-listed. `-v` and `canyonos logs` still have it all.
    """
    tracker = PhaseTracker()
    recent = deque(maxlen=_RECENT_LINES)
    reached_up_marker = False

    # The spinner is exited before the summary panel or the dashboard's own
    # spinner is drawn, and on the way out of a Ctrl+C, so the cursor is restored.
    # A nested spinner wouldn't raise, it would silently render nothing.
    with ui.status("Starting build...") as spinner:
        for line in _drain(lines, state):
            recent.append(line)
            message, done, is_error = tracker.feed(line)
            if is_error:
                break
            if done:
                ui.ok(done)
            if message:
                spinner.update(message)
            if _WORKFLOW_UP_MARKER in line:
                summary_line, all_ready = tracker.agents_ready_message()
                (ui.ok if all_ready else ui.warn)(summary_line)
                reached_up_marker = True
                break

    if reached_up_marker:
        return _deploy_summary(state, api_port, serve)

    _reveal_failure(lines, recent, state)
    return None


def _queued_lines(stream):
    """Feed `stream` into a queue, terminated by None, so reads can time out.

    A failed build leaves the log stream open and silent -- the deploy is only a
    subprocess of the container being tailed -- so blocking on the next line
    would wait forever with nothing left to report.
    """
    lines = queue.Queue()

    def read():
        for line in stream:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    return lines


def _drain(lines, state, deadline=None):
    """Yield log lines until the stream ends, the deploy dies, or `deadline` passes.

    The container's /status is polled on the read timeout rather than per line,
    because the container logs each of those requests into the very stream being
    read -- which would otherwise feed itself.
    """
    misses = 0
    while deadline is None or time.monotonic() < deadline:
        try:
            line = lines.get(timeout=_STATUS_POLL_SECONDS)
        except queue.Empty:
            # Nothing for a while: check the deploy is still alive, since a
            # build that died takes the output with it but not the stream.
            dead, misses = _deploy_is_dead(state, misses)
            if dead:
                return
            continue
        if line is None:
            return
        misses = 0
        if _OWN_REQUEST_MARKER not in line:
            yield line


def _deploy_is_dead(state, misses):
    """Whether the in-container deploy has stopped, over two consecutive checks.

    An unreachable container counts as a miss rather than a verdict, so one
    dropped request doesn't end a deploy that is merely busy.
    """
    status = deploy_status(state["port"])
    if status is not None and status.get("running"):
        return False, 0
    misses += 1
    return misses >= 2, misses


def _reveal_failure(lines, recent, state):
    """Stop hiding: replay what was suppressed, then keep echoing.

    The cause is usually still in flight when the verdict lands, so this keeps
    draining until the container confirms the deploy is gone.
    """
    ui.fail("Deploy failed.")
    ui.blank()
    for buffered in recent:
        print(buffered, end="")

    for line in _drain(lines, state, deadline=time.monotonic() + _REVEAL_GRACE_SECONDS):
        print(line, end="")

    ui.blank()
    ui.hint("Run `canyonos deploy -v` or `canyonos logs` for the full container log.")


def _stream_logs_and_autoserve(state, api_port, serve=True, verbose=False):
    """Tail the GC container's logs, and once they show the workflow is up,
    start the dashboard (unless disabled via `serve=False`) and print where
    everything lives. Log tailing continues afterwards.
    """
    process = subprocess.Popen(
        ["docker", "logs", "-f", state["container_id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        if verbose:
            _tail_verbose(process.stdout, state, api_port, serve)
            return
        lines = _queued_lines(process.stdout)
        if _tail_quiet(lines, state, api_port, serve) is not None:
            # Quiet mode stays attached after the summary so Ctrl+C means the
            # same thing in both modes -- it just swallows what arrives.
            while lines.get() is not None:
                pass
    except KeyboardInterrupt:
        _interrupted()
    finally:
        if process.poll() is None:
            process.terminate()
