"""
Logic for `canyonos test`: smoke-test a project end to end on this machine.

Every agent's `provider` is rewritten to `local` for the duration of the run
(the original file is restored verbatim afterwards), the project is deployed
into the Global Controller container, one query is sent to the workflow's
`/main` endpoint, and its result -- or the error that came back -- is printed.
"""

import json
import os
import time
import urllib.error
import urllib.request

from rich.console import Console

from canyonos.constants import (
    WORKFLOW_ROUTE,
    default_config_path,
    round_trip_yaml,
    workflow_api_port,
    workspace_relative,
)
from canyonos.gc import GCError, deploy_status, post_deploy
from canyonos.init import load_state, quit_existing, run_init
from canyonos.sync import run_sync

DEFAULT_QUERY = "hello"
# Generous: the first deploy of a project builds every agent image from scratch.
READY_TIMEOUT = 900
REQUEST_TIMEOUT = 600
POLL_INTERVAL = 2



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


def _workflow_ready(api_port):
    """True once the workflow's REST API answers at all.

    Any HTTP response counts -- /status/<unknown id> 404s, which still proves
    the server is up and listening.
    """
    url = f"http://127.0.0.1:{api_port}/status/canyonos-test-probe"
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _wait_for_workflow(gc_port, api_port, console):
    deadline = time.time() + READY_TIMEOUT
    with console.status("Building and starting containers..."):
        while time.time() < deadline:
            if _workflow_ready(api_port):
                return True
            if not (deploy_status(gc_port) or {}).get("running", False):
                return False
            time.sleep(POLL_INTERVAL)
    print(f"Timed out after {READY_TIMEOUT}s waiting for the workflow to come up.")
    return False


def _send_query(api_port, query):
    url = f"http://127.0.0.1:{api_port}/{WORKFLOW_ROUTE}"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["request_id"]


def _await_result(api_port, request_id, console):
    url = f"http://127.0.0.1:{api_port}/status/{request_id}"
    deadline = time.time() + REQUEST_TIMEOUT
    with console.status("Running query..."):
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


def run_test(config_path=None, query=None):
    console = Console()
    config_path = config_path or default_config_path()
    query = query or DEFAULT_QUERY

    config_path = workspace_relative(config_path)
    if config_path is None:
        print("Config must be inside the project directory being synced.")
        return 1

    if not os.path.isfile(config_path):
        print(f"Config file not found: {config_path}. Run `canyonos build` first.")
        return 1

    api_port = workflow_api_port(config_path)
    if api_port is None:
        print(f"No agent with `type: workflow` in {config_path}; nothing to test.")
        return 1

    print(f"Testing {config_path} locally (query: {query!r})")
    original_config = _force_local_providers(config_path)

    try:
        run_init()
        if not run_sync():
            return 1

        state = load_state()
        try:
            post_deploy(state["port"], config_path)
        except GCError as e:
            print(e)
            return 1

        if not _wait_for_workflow(state["port"], api_port, console):
            print("The deploy did not come up. Run `canyonos logs` to see why.")
            return 1

        try:
            request_id = _send_query(api_port, query)
        except OSError as e:
            print(f"Could not reach the workflow on port {api_port}: {e}")
            return 1
        result = _await_result(api_port, request_id, console)
    except KeyboardInterrupt:
        print("\nTest cancelled.")
        return 1
    finally:
        with open(config_path, "w") as f:
            f.write(original_config)
        # A smoke test leaves nothing behind: run_init() started this container.
        quit_existing()

    status = result.get("status")
    if status == "done":
        print("Test passed.")
        print(json.dumps(result.get("result"), indent=2))
        return 0

    if status == "error":
        print(f"Test failed: {result.get('error')}")
    else:
        print(f"Test failed: workflow did not finish within {REQUEST_TIMEOUT}s.")
    return 1
