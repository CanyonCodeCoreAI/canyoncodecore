"""
Logic for `canyonos test`: check a project end to end on this machine.

Three phases, each ending the run if it fails:
- The project is deployed locally (every agent's `provider` rewritten to `local` for the duration, the original file restored verbatim afterwards)
- The running containers are checked against what the config declared
- One prompt is sent to the workflow's `/main` endpoint.

Every run tears its own deploy down, pass or fail; a failing one still prints
the Global Controller's log tail first, so there's something to debug.

This file will also need lots of iteration based on what is needed, will expect it to change alot
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from rich.panel import Panel
from rich.text import Text

from canyonos import ui
from canyonos.constants import (
    WORKFLOW_ROUTE,
    default_config_path,
    port_in_use,
    round_trip_yaml,
    workflow_api_port,
    workspace_relative,
)
from canyonos.deploy import run_deploy, workflow_targets
from canyonos.gc import _DEPLOY_CONFLICT, deploy_status
from canyonos.init import load_state, quit_existing
from canyonos.theme import GREEN, WHITE
from canyonos.verify import verify_runtime

TEST_NAMESPACE = "test"
# Where the free-port scan for the test's own api_port/dashboard_port starts --
# clear of the ports a real deploy's config would typically use.
TEST_PORT_START = 9000
DEFAULT_QUERY = "hello"
# `canyonos test` stubs the in-container LLM proxy by default so a smoke test
# never calls a real LLM (no credentials, no token cost). Every model call
# returns this text; pass --real-llm to use the actual provider instead.
DEFAULT_LLM_STUB = "test"
READY_TIMEOUT = 60
REQUEST_TIMEOUT = 60
SUBMIT_TIMEOUT = 30
POLL_INTERVAL = 2
LOG_TAIL_LINES = 40


def _free_test_port(reserved):
    """First free port at/after TEST_PORT_START not already claimed this run."""
    port = TEST_PORT_START
    while port_in_use(port) or port in reserved:
        port += 1
    reserved.add(port)
    return port


def _force_local_providers(config_path):
    """Set every agent's provider to `local`, and pin the workflow's api_port/
    dashboard_port to free ports starting at TEST_PORT_START -- so a test run
    never binds the project's configured ports and can't collide with a real
    deploy. Returns the original file text."""
    with open(config_path) as f:
        original = f.read()

    yaml_rt = round_trip_yaml()
    data = yaml_rt.load(original)

    reserved_ports = set()
    for agent in data.get("agents") or []:
        agent["provider"] = "local"
        if agent.get("type") == "workflow":
            agent["api_port"] = _free_test_port(reserved_ports)
            agent["dashboard_port"] = _free_test_port(reserved_ports)

    with open(config_path, "w") as f:
        yaml_rt.dump(data, f)

    return original


def _workflow_ready(host, port):
    """True once the workflow's REST API answers at all.

    Any HTTP response counts -- /status/<unknown id> 404s, which still proves
    the server is up and listening.
    """
    url = f"http://{host}:{port}/status/canyonos-test-probe"
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _wait_for_workflow(gc_port, api_port):
    deadline = time.time() + READY_TIMEOUT
    with ui.status("Building images and starting containers..."):
        while time.time() < deadline:
            if _workflow_ready("127.0.0.1", api_port):
                return
            if not (deploy_status(gc_port) or {}).get("running", False):
                raise RuntimeError("The deploy stopped before the workflow came up.")
            time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Timed out after {READY_TIMEOUT}s waiting for the workflow to come up.")


def _send_query(host, port, query):
    url = f"http://{host}:{port}/{WORKFLOW_ROUTE}"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT) as resp:
        return json.loads(resp.read())["request_id"]


def _await_result(host, port, request_id):
    url = f"http://{host}:{port}/status/{request_id}"
    deadline = time.time() + REQUEST_TIMEOUT
    with ui.status("Running query..."):
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read())
                if data.get("status") in ("done", "error"):
                    return data
            except OSError:
                # A blip while the workflow is busy; keep polling until the deadline.
                pass
            time.sleep(POLL_INTERVAL)
    return {"status": "timeout"}


def _log_tail(container_id):
    result = subprocess.run(
        ["docker", "logs", "--tail", str(LOG_TAIL_LINES), container_id],
        capture_output=True,
        text=True,
    )
    return (result.stdout + result.stderr).strip() or None


class _Run:
    """One `canyonos test` invocation: the phases it got through, and what they found."""

    def __init__(self, query):
        self.query = query
        self.started = time.monotonic()
        # Only once a deploy is under way is the container worth keeping and its
        # log worth reading; before that it holds nothing about the failure.
        self.deploy_started = False
        self.phases = []
        self.runtime = None
        self.endpoint = None
        self.result = None
        self.error = None
        self.log_tail = None

    def begin(self, name, number, title):
        """Open a phase, recorded as failed until `done` says otherwise."""
        self.phases.append({"name": name, "ok": False, "detail": None})
        ui.blank()
        ui.say(f"[{number}/3] {title}")

    def done(self, detail=None):
        self.phases[-1].update(ok=True, detail=detail)

    def failed(self, detail):
        if self.phases:
            self.phases[-1]["detail"] = detail

    def elapsed(self):
        return round(time.monotonic() - self.started, 3)


def _deploy_locally(run, config_path, api_port, llm_stub=DEFAULT_LLM_STUB):
    run.begin("deploy", 1, "Deploy locally")
    # When stubbing, hand the flag to the GC container; the local runtime
    # forwards it into every agent so their LLM calls are replaced with canned
    # text (see canyonos_core/llm_proxy/stub.py).
    extra_env = {"CANYONOS_LLM_STUB_TEXT": llm_stub} if llm_stub else None
    if llm_stub:
        ui.say(f"LLM stub on: every model call returns {llm_stub!r} (no real LLM). Pass --real-llm to disable.")

    # quiet=True: skip `canyonos deploy`'s own log-tail/summary UI, we do our
    # own HTTP readiness check below instead. serve=True still brings the
    # dashboard's LLM proxy up, quietly, for code that calls it directly.
    state = run_deploy(config_path, serve=True, quiet=True, extra_env=extra_env, banner=False)
    run.deploy_started = True

    _wait_for_workflow(state["port"], api_port)
    run.done(f"Global Controller on port {state['port']}")
    return state


def _verify_runtime(run, config_path, gc_port):
    run.begin("verify_runtime", 2, "Verify runtime")
    run.runtime = verify_runtime(config_path, gc_port)
    run.done(f"{len(run.runtime['agents'])} agent(s) up")


def _query(run, gc_port, api_port):
    run.begin("query", 3, "Query the workflow")
    targets = workflow_targets(gc_port, api_port)
    if not targets:
        raise RuntimeError("The deploy reported no workflow endpoint to query.")

    _, host, port = targets[0]
    run.endpoint = f"http://{host}:{port}/{WORKFLOW_ROUTE}"
    ui.say(f"POST {run.endpoint}  {json.dumps({'query': run.query})}")

    try:
        request_id = _send_query(host, port, run.query)
    except OSError as e:
        raise RuntimeError(f"Could not reach the workflow at {run.endpoint}: {e}") from None

    data = _await_result(host, port, request_id)
    status = data.get("status")
    if status == "error":
        raise RuntimeError(data.get("error") or "the workflow returned an error.")
    if status != "done":
        raise RuntimeError(f"The workflow did not finish within {REQUEST_TIMEOUT}s.")

    run.result = data.get("result")
    run.done(f"answered in {run.elapsed()}s")


def _refuse_if_deploy_running():
    """Bail out before touching anything if a REAL deploy is already up --
    otherwise `_deploy_locally` would tear it down via `run_init`'s own cleanup
    only to fail later for an unrelated reason.

    Always checked against the real (non-namespaced) state file, even though
    this runs with CANYONOS_NAMESPACE=test already set -- a leftover from a
    previous failed test run is not a conflict, it's exactly what run_init's
    own quit_existing() already self-heals a few lines later.
    """
    original_namespace = os.environ.pop("CANYONOS_NAMESPACE", None)
    try:
        state = load_state()
    except FileNotFoundError:
        return
    finally:
        if original_namespace is not None:
            os.environ["CANYONOS_NAMESPACE"] = original_namespace

    if (deploy_status(state["port"]) or {}).get("running", False):
        raise RuntimeError(_DEPLOY_CONFLICT)


def _run_test(run, llm_stub=DEFAULT_LLM_STUB):
    """Walk the three phases, restoring the config whatever happens."""
    _refuse_if_deploy_running()

    config_path = workspace_relative(default_config_path())
    if config_path is None:
        raise RuntimeError("Config must be inside the project directory being synced.")
    if not os.path.isfile(config_path):
        raise RuntimeError(f"No config at {config_path}. Run `canyonos build` first.")

    api_port = workflow_api_port(config_path)
    if api_port is None:
        raise RuntimeError(f"No agent with `type: workflow` in {config_path}; nothing to test.")

    original_config = _force_local_providers(config_path)
    # Re-read: _force_local_providers just pinned api_port to a fresh free port.
    api_port = workflow_api_port(config_path)
    try:
        state = _deploy_locally(run, config_path, api_port, llm_stub=llm_stub)
        _verify_runtime(run, config_path, state["port"])
        _query(run, state["port"], api_port)
    finally:
        with open(config_path, "w") as f:
            f.write(original_config)


# ------------------------------------------------------------------ #
#  Output                                                             #
# ------------------------------------------------------------------ #


def _summary_body(run):
    body = Text()
    body.append("Query      ", "dim")
    body.append(run.query, WHITE)
    if run.endpoint:
        body.append("\nEndpoint   ", "dim")
        body.append(run.endpoint, WHITE)
    body.append("\nElapsed    ", "dim")
    body.append(f"{run.elapsed()}s", WHITE)

    body.append("\n")
    for phase in run.phases:
        body.append("\n")
        body.append("✓ " if phase["ok"] else "✗ ", GREEN if phase["ok"] else "bold red")
        body.append(f"{phase['name']:<16}", WHITE)
        # The failing phase's detail is the error, spelled out below in full.
        body.append(phase["detail"] if phase["ok"] else "", "dim")

    body.append("\n\n")
    if run.error is None:
        body.append("Result     ", "dim")
        body.append(json.dumps(run.result, indent=2), WHITE)
    else:
        body.append(run.error, "bold red")
    return body


def _print_summary(run):
    passed = run.error is None
    ui.blank()
    ui.panel(
        Panel(
            _summary_body(run),
            title=f"[bold {GREEN}]Test passed[/]" if passed else "[bold red]Test failed[/]",
            title_align="left",
            border_style=GREEN if passed else "red",
            padding=(1, 4),
        )
    )
    ui.blank()


def _readable_result(result):
    """`result` unwrapped to its plain value when it's just one field -- the
    common case (e.g. `{"reply": "..."}`) reads far better than raw JSON.
    """
    if isinstance(result, dict) and len(result) == 1:
        value = next(iter(result.values()))
        if isinstance(value, str):
            return value
    return json.dumps(result, indent=2)


def _print_io(run):
    """A short, scannable input/output pair -- the main panel's own Result field
    is the full raw JSON, which gets unreadable fast for a nested result.
    """
    body = Text()
    body.append("Input   ", "dim")
    body.append(run.query, WHITE)
    body.append("\nOutput  ", "dim")
    body.append(_readable_result(run.result), WHITE)
    ui.panel(
        Panel(
            body,
            title=f"[bold {GREEN}]Input / Output[/]",
            title_align="left",
            border_style=GREEN,
            padding=(1, 4),
        )
    )
    ui.blank()


def _print_failure_logs(run):
    ui.hint(f"last {LOG_TAIL_LINES} lines of the Global Controller log:")
    ui.say(run.log_tail)
    ui.blank()


def _payload(run):
    return {
        "ok": run.error is None,
        "query": run.query,
        "elapsed_s": run.elapsed(),
        "phases": run.phases,
        "runtime": run.runtime,
        "result": run.result,
        "error": run.error,
        "log_tail": run.log_tail,
    }


def run_test(prompt=None, as_json=False, llm_stub=DEFAULT_LLM_STUB):
    run = _Run(prompt or DEFAULT_QUERY)
    ui.set_quiet(as_json)
    # Own namespace for the whole run (state file, GC/Redis/agent container
    # names) so this never touches -- or gets confused by -- a real deploy's.
    original_namespace = os.environ.get("CANYONOS_NAMESPACE")
    os.environ["CANYONOS_NAMESPACE"] = TEST_NAMESPACE

    try:
        try:
            _run_test(run, llm_stub=llm_stub)
        except KeyboardInterrupt:
            run.error = "cancelled by user"
        except RuntimeError as e:
            # Every phase raises RuntimeError with a message fit for either output
            # mode: docker unreachable, validation failure, port in use, workflow
            # timeout, etc. `--json` needs it inside the payload either way.
            run.error = str(e)

        if run.error is not None:
            run.failed(run.error)

        if run.deploy_started:
            # Read the log before tearing anything down -- it's the only trace
            # of a failure left once quit_existing() below removes the container.
            try:
                run.log_tail = _log_tail(load_state()["container_id"])
            except (FileNotFoundError, OSError):
                pass
            # TODO: always tearing down trades away inspecting a live failed deploy -- rework once that's needed again.
            quit_existing()
        # else: failed before this run ever started its own deploy (e.g. bad
        # config, or `_refuse_if_deploy_running` above) -- nothing of ours to
        # clean up, so leave whatever was already there alone.

        if as_json:
            print(json.dumps(_payload(run), indent=2))
        else:
            _print_summary(run)
            if run.error is None:
                _print_io(run)
            elif run.log_tail:
                _print_failure_logs(run)

        return 0 if run.error is None else 1
    finally:
        if original_namespace is None:
            os.environ.pop("CANYONOS_NAMESPACE", None)
        else:
            os.environ["CANYONOS_NAMESPACE"] = original_namespace
        ui.set_quiet(False)
