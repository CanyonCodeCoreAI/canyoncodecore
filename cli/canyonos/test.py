"""
Logic for `canyonos test`: check a project end to end on this machine.

Four phases, each ending the run if it fails: the `.car/` artifact `canyonos
build` produced is verified statically, the project is deployed locally (every
agent's `provider` rewritten to `local` for the duration, the original file
restored verbatim afterwards), the running containers are checked against what
the config declared, and one prompt is sent to the workflow's `/main` endpoint.

A passing run leaves nothing behind. A failing one leaves the Global Controller
container up, with the tail of its log, so there is something left to debug.
"""

import json
import os
import socket
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
    round_trip_yaml,
    workflow_api_port,
    workspace_relative,
)
from canyonos.deploy import workflow_targets
from canyonos.gc import GCError, deploy_status, post_deploy
from canyonos.init import load_state, quit_existing, run_init
from canyonos.sync import run_sync
from canyonos.theme import GREEN, WHITE
from canyonos.verify import (
    ARTIFACT_DIR,
    VerificationError,
    verify_build_artifact,
    verify_runtime,
)

DEFAULT_QUERY = "hello"
# `canyonos test` stubs the in-container LLM proxy by default so a smoke test
# never calls a real LLM (no credentials, no token cost). Every model call
# returns this text; pass --real-llm to use the actual provider instead.
DEFAULT_LLM_STUB = "test"
# Generous: the first deploy of a project builds every agent image from scratch.
READY_TIMEOUT = 900
REQUEST_TIMEOUT = 600
SUBMIT_TIMEOUT = 30
POLL_INTERVAL = 2
LOG_TAIL_LINES = 40


def _force_local_providers(config_path):
    """Set every agent's provider to `local`. Returns the original file text."""
    with open(config_path) as f:
        original = f.read()

    yaml_rt = round_trip_yaml()
    data = yaml_rt.load(original)

    for agent in data.get("agents") or []:
        agent["provider"] = "local"

    with open(config_path, "w") as f:
        yaml_rt.dump(data, f)

    return original


def _port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


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
                raise _TestFailed("The deploy stopped before the workflow came up.")
            time.sleep(POLL_INTERVAL)
    raise _TestFailed(f"Timed out after {READY_TIMEOUT}s waiting for the workflow to come up.")


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


class _TestFailed(Exception):
    """Ends the run early, carrying a message fit for either output mode."""


class _Run:
    """One `canyonos test` invocation: the phases it got through, and what they found."""

    def __init__(self, query):
        self.query = query
        self.started = time.monotonic()
        # Only once a deploy is under way is the container worth keeping and its
        # log worth reading; before that it holds nothing about the failure.
        self.deploy_started = False
        self.phases = []
        self.validation = None
        self.runtime = None
        self.endpoint = None
        self.result = None
        self.error = None
        self.log_tail = None

    def begin(self, name, number, title):
        """Open a phase, recorded as failed until `done` says otherwise."""
        self.phases.append({"name": name, "ok": False, "detail": None})
        ui.blank()
        ui.say(f"[{number}/4] {title}")

    def done(self, detail=None):
        self.phases[-1].update(ok=True, detail=detail)

    def failed(self, detail):
        if self.phases:
            self.phases[-1]["detail"] = detail

    def elapsed(self):
        return round(time.monotonic() - self.started, 3)


def _verify_build(run, config_path):
    run.begin("verify_build", 1, "Verify build artifact")

    # A project ported before the .car layout keeps its config at the top level;
    # there is no build artifact to check, so the deploy phases still run.
    if not config_path.startswith(f"{ARTIFACT_DIR}{os.sep}"):
        ui.warn(f"No `{ARTIFACT_DIR}/` artifact -- deploying {config_path} as it is.")
        ui.hint("  -> `canyonos build` produces one, and gives this phase something to check.")
        run.done("skipped: no .car/ artifact")
        return

    try:
        run.validation = verify_build_artifact()
    except VerificationError as e:
        raise _TestFailed(str(e)) from None
    stale = len(run.validation["stale"])
    run.done(f"{run.validation['warnings']} warning(s), {stale} stale source(s)")


def _deploy_locally(run, config_path, api_port, llm_stub=DEFAULT_LLM_STUB):
    run.begin("deploy", 2, "Deploy locally")
    # When stubbing, hand the flag to the GC container; the local runtime
    # forwards it into every agent so their LLM calls are replaced with canned
    # text (see canyonos_core/llm_proxy/stub.py).
    extra_env = {"CANYONOS_LLM_STUB_TEXT": llm_stub} if llm_stub else None
    if llm_stub:
        ui.say(f"LLM stub on: every model call returns {llm_stub!r} (no real LLM). Pass --real-llm to disable.")
    run_init(banner=False, extra_env=extra_env)

    if not run_sync():
        raise _TestFailed("Could not sync the project into the container.")

    # Only the gRPC host port is bumped when a port is taken (the local runtime's
    # launch retry), so an occupied api_port dies 50 attempts later as "no free
    # port found". `canyonos serve` also starts looking for its web port at 8080.
    if _port_in_use(api_port):
        raise _TestFailed(
            f"Port {api_port} is already in use, and the workflow needs it. Free it "
            f"(`canyonos quit` stops a previous deploy) or change `api_port` in {config_path}."
        )

    state = load_state()
    try:
        post_deploy(state["port"], config_path)
    except GCError as e:
        raise _TestFailed(str(e)) from None
    run.deploy_started = True

    _wait_for_workflow(state["port"], api_port)
    run.done(f"Global Controller on port {state['port']}")
    return state


def _verify_runtime(run, config_path, gc_port):
    run.begin("verify_runtime", 3, "Verify runtime")
    try:
        run.runtime = verify_runtime(config_path, gc_port)
    except VerificationError as e:
        raise _TestFailed(str(e)) from None
    run.done(f"{len(run.runtime['agents'])} agent(s) up")


def _query(run, gc_port, api_port):
    run.begin("query", 4, "Query the workflow")
    targets = workflow_targets(gc_port, api_port)
    if not targets:
        raise _TestFailed("The deploy reported no workflow endpoint to query.")

    _, host, port = targets[0]
    run.endpoint = f"http://{host}:{port}/{WORKFLOW_ROUTE}"
    ui.say(f"POST {run.endpoint}  {json.dumps({'query': run.query})}")

    try:
        request_id = _send_query(host, port, run.query)
    except OSError as e:
        raise _TestFailed(f"Could not reach the workflow at {run.endpoint}: {e}") from None

    data = _await_result(host, port, request_id)
    status = data.get("status")
    if status == "error":
        raise _TestFailed(data.get("error") or "the workflow returned an error.")
    if status != "done":
        raise _TestFailed(f"The workflow did not finish within {REQUEST_TIMEOUT}s.")

    run.result = data.get("result")
    run.done(f"answered in {run.elapsed()}s")


def _run_test(run, llm_stub=DEFAULT_LLM_STUB):
    """Walk the four phases, restoring the config whatever happens."""
    config_path = workspace_relative(default_config_path())
    if config_path is None:
        raise _TestFailed("Config must be inside the project directory being synced.")
    if not os.path.isfile(config_path):
        raise _TestFailed(f"No config at {config_path}. Run `canyonos build` first.")

    _verify_build(run, config_path)

    api_port = workflow_api_port(config_path)
    if api_port is None:
        raise _TestFailed(f"No agent with `type: workflow` in {config_path}; nothing to test.")

    original_config = _force_local_providers(config_path)
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


def _print_failure_logs(run):
    if run.log_tail:
        ui.hint(f"last {LOG_TAIL_LINES} lines of the Global Controller log:")
        ui.say(run.log_tail)
        ui.blank()
    ui.hint("Containers left running for inspection: `canyonos logs` | `canyonos quit`")


def _payload(run):
    return {
        "ok": run.error is None,
        "query": run.query,
        "elapsed_s": run.elapsed(),
        "phases": run.phases,
        "validation": run.validation,
        "runtime": run.runtime,
        "result": run.result,
        "error": run.error,
        "log_tail": run.log_tail,
    }


def run_test(prompt=None, as_json=False, llm_stub=DEFAULT_LLM_STUB):
    run = _Run(prompt or DEFAULT_QUERY)
    ui.set_quiet(as_json)

    try:
        container_live = False
        try:
            _run_test(run, llm_stub=llm_stub)
        except _TestFailed as e:
            run.error = str(e)
        except KeyboardInterrupt:
            run.error = "cancelled by user"
        except RuntimeError as e:
            # Docker unreachable, image pull failed, no free port: all carry a
            # readable message, and `--json` needs it inside the payload.
            run.error = str(e)

        if run.error is not None:
            run.failed(run.error)

        if run.error is not None and run.deploy_started:
            # Read the log before anything else touches the container, and leave
            # it running -- a torn-down deploy can't be diagnosed.
            try:
                run.log_tail = _log_tail(load_state()["container_id"])
                container_live = True
            except (FileNotFoundError, OSError):
                pass
        else:
            quit_existing()

        if as_json:
            print(json.dumps(_payload(run), indent=2))
        else:
            _print_summary(run)
            if container_live:
                _print_failure_logs(run)

        return 0 if run.error is None else 1
    finally:
        ui.set_quiet(False)
