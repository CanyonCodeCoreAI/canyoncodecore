"""
Logic for `canyonos test`: check a project end to end on this machine.

Three phases, each ending the run if it fails:
- The project is deployed locally (every agent's `provider` rewritten to `local` for the duration, the original file restored verbatim afterwards)
- The running containers are checked against what the config declared
- One prompt is sent to the workflow's `/main` endpoint.

A passing run leaves nothing behind. A failing one leaves the Global Controller
container up, with the tail of its log, so there is something left to debug.

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
    DEFAULT_QUERY_PARAM,
    WORKFLOW_ROUTE,
    default_config_path,
    round_trip_yaml,
    workflow_api_port,
    workflow_entrypoint,
    workspace_relative,
)
from canyonos.deploy import run_deploy, workflow_targets
from canyonos.gc import deploy_status
from canyonos.init import load_state, quit_existing
from canyonos.theme import GREEN, WHITE
from canyonos.verify import verify_runtime

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


def _query_route_and_body(config_path, query):
    """(route, body dict) for the test POST, read from the workflow function's own
    signature so the body keys match its parameter names.

    The deployed workflow splats the whole JSON body as kwargs
    (`workflow_fn(**body)`), so a hardcoded `{"query": ...}` 500s on any
    entrypoint whose first parameter isn't named `query`. We map the test
    prompt onto the real first parameter and fill the rest from their defaults
    (falling back to the prompt when a param has none).
    """
    entrypoint = workflow_entrypoint(config_path)
    if not entrypoint:
        return WORKFLOW_ROUTE, {DEFAULT_QUERY_PARAM: query}

    fn_name, params = entrypoint
    if not params:
        return fn_name, {DEFAULT_QUERY_PARAM: query}

    body = {}
    for i, (name, default) in enumerate(params):
        body[name] = query if i == 0 else (default if default is not None else query)
    return fn_name, body


def _send_query(host, port, route, body):
    url = f"http://{host}:{port}/{route}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT) as resp:
        payload = resp.read()
    try:
        return json.loads(payload)["request_id"]
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(
            f"The workflow accepted the request but returned an unexpected response "
            f"(no request_id): {e}"
        ) from None


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
            except (OSError, ValueError):
                # A network blip or a malformed body while the workflow is busy;
                # keep polling until the deadline rather than crashing the run.
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
        # True when we queried a deploy that was already up rather than standing
        # up our own -- it must not be torn down afterwards, it isn't ours.
        self.against_existing = False
        self.phases = []
        self.runtime = None
        self.endpoint = None
        self.result = None
        self.error = None
        self.log_tail = None

    def begin(self, name, number, title, total=3):
        """Open a phase, recorded as failed until `done` says otherwise."""
        self.phases.append({"name": name, "ok": False, "detail": None})
        ui.blank()
        ui.say(f"[{number}/{total}] {title}")

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


def _query(run, gc_port, api_port, config_path, number=3, total=3):
    run.begin("query", number, "Query the workflow", total=total)
    targets = workflow_targets(gc_port, api_port)
    if not targets:
        raise RuntimeError("The deploy reported no workflow endpoint to query.")

    _, host, port = targets[0]
    route, body = _query_route_and_body(config_path, run.query)
    run.endpoint = f"http://{host}:{port}/{route}"
    ui.say(f"POST {run.endpoint}  {json.dumps(body)}")

    try:
        request_id = _send_query(host, port, route, body)
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


def _existing_deploy():
    """The running deploy's state if one is already up, else None."""
    try:
        state = load_state()
    except FileNotFoundError:
        return None
    if (deploy_status(state["port"]) or {}).get("running", False):
        return state
    return None


def _run_test(run, llm_stub=DEFAULT_LLM_STUB):
    """Query the workflow, standing up our own local deploy first unless one is
    already up. The config is restored whatever happens."""
    config_path = workspace_relative(default_config_path())
    if config_path is None:
        raise RuntimeError("Config must be inside the project directory being synced.")
    if not os.path.isfile(config_path):
        raise RuntimeError(f"No config at {config_path}. Run `canyonos build` first.")

    api_port = workflow_api_port(config_path)
    if api_port is None:
        raise RuntimeError(f"No agent with `type: workflow` in {config_path}; nothing to test.")

    existing = _existing_deploy()
    if existing is not None:
        # A deploy is already up: query it exactly as it stands (its own
        # providers and LLM, no local flip, no stub) instead of tearing it down
        # to stand up our own. This is the only phase, and we leave it running.
        run.against_existing = True
        ui.say("A deploy is already up -- querying it as it stands (providers and LLM unchanged).")
        _query(run, existing["port"], api_port, config_path, number=1, total=1)
        return

    original_config = _force_local_providers(config_path)
    try:
        state = _deploy_locally(run, config_path, api_port, llm_stub=llm_stub)
        _verify_runtime(run, config_path, state["port"])
        _query(run, state["port"], api_port, config_path)
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
    if run.log_tail:
        ui.hint(f"last {LOG_TAIL_LINES} lines of the Global Controller log:")
        ui.say(run.log_tail)
        ui.blank()
    ui.hint("Containers left running for inspection: `canyonos logs` | `canyonos quit`")


def _payload(run):
    return {
        "ok": run.error is None,
        "query": run.query,
        "against_existing_deploy": run.against_existing,
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

    try:
        container_live = False
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

        if run.error is not None and run.deploy_started:
            # Read the log before anything else touches the container, and leave
            # it running -- a torn-down deploy can't be diagnosed.
            try:
                run.log_tail = _log_tail(load_state()["container_id"])
                container_live = True
            except (FileNotFoundError, OSError):
                pass
        elif run.error is None and not run.against_existing:
            quit_existing()
        # else: either we queried a deploy that was already up (not ours to tear
        # down), or we failed before this run ever started its own deploy (e.g.
        # bad config) -- leave whatever was already there alone.

        if as_json:
            print(json.dumps(_payload(run), indent=2))
        else:
            _print_summary(run)
            if run.error is None:
                _print_io(run)
            if container_live:
                _print_failure_logs(run)

        return 0 if run.error is None else 1
    finally:
        ui.set_quiet(False)
