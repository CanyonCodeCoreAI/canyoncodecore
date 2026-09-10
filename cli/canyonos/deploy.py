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

import json
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
    DEFAULT_QUERY_PARAM,
    WORKFLOW_ROUTE,
    default_config_path,
    missing_config_message,
    port_in_use,
    public_ip,
    workflow_api_port,
    workflow_entrypoint,
    workspace_relative,
)
from canyonos.gc import GCError, deploy_status, post_deploy, workflow_endpoints
from canyonos.theme import GREEN, WHITE
from canyonos.init import load_state, run_init
from canyonos.serve import serve_dashboard
from canyonos.sync import run_sync

# Test seams: a failed-build test monkeypatches both down so it doesn't have to
# wait out the real poll/grace windows.
_STATUS_POLL_SECONDS = 2.0
_REVEAL_GRACE_SECONDS = 30.0

# Substrings that mean the in-container deploy hit something fatal. `WARNING:` is
# deliberately absent: the OTel-not-configured notice and stub_generator's
# "Warning:" lines are benign and fire on nearly every run. `CRITICAL:` is the
# levelname prefix logging emits for the GlobalController's pre-exit failures
# (a Redis port collision, a missing Docker), which otherwise vanish in quiet mode.
_ERROR_MARKERS = (
    "ERROR:",
    "CRITICAL:",
    "Traceback (most recent call last):",
    "ERROR: failed to solve",
    "process did not complete successfully",
)

# Loggers whose own ERROR: lines are expected, self-recovering noise -- not a
# reason to abort the deploy. Checked before _ERROR_MARKERS so they never
# match: the OTel exporter logs at ERROR: when a destination (the dashboard's
# ingest) isn't reachable yet, which is normal on every cold deploy since
# `canyonos serve` hasn't been started at that point -- it retries and
# recovers on its own once the dashboard comes up.
_BENIGN_ERROR_PREFIXES = ("ERROR:opentelemetry.",)

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
        if any(prefix in line for prefix in _BENIGN_ERROR_PREFIXES):
            return None, None, False
        if any(marker in line for marker in _ERROR_MARKERS):
            return None, None, True

        count = re.search(r"Building (\d+) Docker image\(s\) via", line)
        if count:
            self.spinner = f"Building {count.group(1)} images..."
            return self.spinner, None, False

        replicas = re.search(r"Waiting for (\d+) replica\(s\) to become healthy", line)
        if replicas:
            self.replicas_total = int(replicas.group(1))
            self.spinner = self._agent_progress()
            return self.spinner, None, False

        # Matched on the endpoint, since the name repeats across an agent's replicas.
        ready = re.search(r"Controller (\S+ \([^)]+\)) is ready\.", line)
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


def run_deploy(config_path=None, serve=True, verbose=False, quiet=False, extra_env=None, banner=True):
    """`quiet` skips the log-tail/dashboard UI and returns the GC state right
    after the deploy is triggered -- for a caller (`canyonos test`) that wants
    its own readiness check instead of this command's own output.
    """
    # Left as None when unset: canyonos resolves the artifact layout itself.
    if config_path is not None:
        config_path = workspace_relative(config_path)
        if config_path is None:
            raise RuntimeError("Config must be inside the project directory being synced.")

    # The same path canyonos will resolve in the container. Checked before
    # run_init(), so a project that has nothing to deploy is turned away
    # without first tearing down whatever is running.
    resolved_config = config_path or default_config_path()
    missing = missing_config_message(resolved_config)
    if missing:
        ui.fail(missing)
        return None

    run_init(banner=banner, extra_env=extra_env)

    # Copy the current project into the container before building/deploying.
    if not run_sync():
        raise RuntimeError("Could not sync the project into the container.")

    state = load_state()

    # Read for display only -- canyonos resolves the path it actually deploys.
    api_port = workflow_api_port(resolved_config)

    # Checked here, after run_init() has already torn down any previous deploy,
    # so a still-live prior run doesn't read as an unrelated conflict.
    if api_port is not None and port_in_use(api_port):
        raise RuntimeError(
            f"Port {api_port} is already in use, and the workflow needs it. Free it "
            f"or change `api_port` in {resolved_config}."
        )

    try:
        post_deploy(state["port"], config_path)
    except GCError as e:
        raise RuntimeError(str(e)) from None

    if quiet:
        # Still bring the dashboard up so anything reachable only through its
        # LLM proxy (e.g. a guardrail calling the OpenAI SDK directly) works
        # under `canyonos test` too -- just skip the log-tail/summary UI.
        if serve:
            _start_dashboard()
        return state

    _stream_logs_and_autoserve(state, api_port, resolved_config, serve=serve, verbose=verbose)
    return state


def _display_host(host):
    """Substitute this machine's own public IP for a loopback host, when discoverable.

    A workflow placed on *this* machine reports `127.0.0.1`/`localhost` -- correct
    for curling from the box itself, but useless from anywhere else (e.g. an EC2
    deploy meant to be queried from a laptop). Falls back to `127.0.0.1` off EC2
    (or if the metadata lookup fails), same as before. A workflow placed on a
    *different* machine already reports its own real host and passes through
    unchanged.
    """
    if host not in ("127.0.0.1", "localhost"):
        return host
    return public_ip() or "127.0.0.1"


def workflow_targets(gc_port, api_port):
    """(name, host, port) for each deployed workflow.

    The container reports the address it actually placed each workflow at, so a
    workflow running on another machine shows that machine's public IP. The
    local port mapping is the fallback when it reports nothing.
    """
    targets = [
        (
            endpoint.get("name"),
            _display_host(endpoint["host"]),
            endpoint["port"],
        )
        for endpoint in workflow_endpoints(gc_port)
        if endpoint.get("host") and endpoint.get("port")
    ]
    if targets:
        return targets
    return [(None, _display_host("127.0.0.1"), api_port)] if api_port else []


def _example_route_and_body(config_path):
    """(route, body dict) for the curl example -- read from the workflow function's
    own signature when possible, falling back to the historical `main`/`query` shape."""
    entrypoint = workflow_entrypoint(config_path)
    if not entrypoint:
        return WORKFLOW_ROUTE, {DEFAULT_QUERY_PARAM: "your question here"}

    fn_name, params = entrypoint
    if not params:
        return fn_name, {DEFAULT_QUERY_PARAM: "your question here"}
    return fn_name, {name: default if default is not None else "<value>" for name, default in params}


def _curl_example(url, body):
    """A single-line, directly copy-pasteable `curl -X POST ...` command.

    Deliberately not split across `\\`-continued lines or pretty-printed JSON --
    a multi-line block is easy to mangle depending on what actually receives the
    paste (some terminals/chat boxes drop the backslashes or the newlines), and
    a single line always works no matter where it lands.
    """
    compact_body = json.dumps(body)
    return f'curl -X POST {url} -H "Content-Type: application/json" -d \'{compact_body}\''


def _summary_body(dashboard_url, targets, config_path):
    """ The contents that go inside the deploy panel"""
    body = Text()
    body.append("Dashboard  ", "dim")
    if dashboard_url:
        body.append(dashboard_url, f"bold {GREEN}")
    else:
        body.append("not running -- start it with `canyonos serve`", WHITE)

    route, example = _example_route_and_body(config_path)
    for name, host, port in targets:
        base = f"http://{host}:{port}"
        body.append("\n")
        if name:
            body.append(f"\n{name}", f"bold {WHITE}")
        body.append("\n")
        body.append(_curl_example(f"{base}/{route}", example), WHITE)
        body.append("\npoll       ", "dim")
        body.append(f"{base}/status/<request_id>", WHITE)
        if host not in ("127.0.0.1", "localhost"):
            body.append(f"\n           needs inbound TCP {port} open on {host}", "dim")
    return body


def print_deploy_summary(dashboard_url, targets, config_path):
    """The one screen printed once everything is up: dashboard and workflow endpoints.

    Under `-v` it is printed again on exit, because the log tail continues
    afterwards and would otherwise scroll it out of sight. Quiet mode prints
    nothing after it, so once is enough.
    """
    ui.blank()
    ui.panel(
        Panel(
            _summary_body(dashboard_url, targets, config_path),
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


def _deploy_summary(state, api_port, config_path, serve):
    summary = (
        _start_dashboard() if serve else None,
        workflow_targets(state["port"], api_port),
        config_path,
    )
    print_deploy_summary(*summary)
    ui.hint("Tailing logs now, press Ctrl+C to stop. Run `canyonos stop` to stop the workflow.")
    return summary


def _interrupted(summary=None):
    ui.blank()
    ui.say("Stopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
    ui.hint("To resubscribe to log stream run `canyonos logs`.")
    if summary is not None:
        print_deploy_summary(*summary)


def _tail_verbose(stream, state, api_port, config_path, serve):
    """Every log line, verbatim -- what `-v` restores.

    Ctrl+C reprints the summary here but not in quiet mode: only this tail keeps
    printing past it, so only here has it scrolled out of sight.
    """
    summary = None
    try:
        for line in stream:
            print(line, end="")
            # Logged exactly once, right after the workflow finishes coming up.
            if summary is None and "Global controller started, polling every" in line:
                summary = _deploy_summary(state, api_port, config_path, serve)
    except KeyboardInterrupt:
        _interrupted(summary)


def _tail_quiet(lines, state, api_port, config_path, serve):
    """Only the phase transitions, until the workflow is up or something fails.

    Nothing is echoed raw: the buildx transcript, canyonos' bare prints and grpc's
    stderr have no common prefix to filter on, so anything unrecognized is
    dropped rather than allow-listed. `-v` and `canyonos logs` still have it all.
    """
    tracker = PhaseTracker()
    # 200 is enough to hold a buildx failure block plus a Python traceback;
    # 40 (what `canyonos test` tails) truncates both.
    recent = deque(maxlen=200)
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
            # Logged exactly once, right after the workflow finishes coming up.
            if "Global controller started, polling every" in line:
                summary_line, all_ready = tracker.agents_ready_message()
                (ui.ok if all_ready else ui.warn)(summary_line)
                reached_up_marker = True
                break

    if reached_up_marker:
        return _deploy_summary(state, api_port, config_path, serve)

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
        # Otherwise the container logs its own polling into the stream being read.
        if "GET /status HTTP/1.1" not in line:
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


def _stream_logs_and_autoserve(state, api_port, config_path, serve=True, verbose=False):
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
            _tail_verbose(process.stdout, state, api_port, config_path, serve)
            return
        lines = _queued_lines(process.stdout)
        if _tail_quiet(lines, state, api_port, config_path, serve) is not None:
            # Quiet mode stays attached after the summary so Ctrl+C means the
            # same thing in both modes -- it just swallows what arrives.
            while lines.get() is not None:
                pass
    except KeyboardInterrupt:
        _interrupted()
    finally:
        if process.poll() is None:
            process.terminate()
