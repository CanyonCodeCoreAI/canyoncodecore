"""
Logic for `canyonos doctor`: a checklist of environment checks, split into a
pre-deploy view (no Global Controller deploy running yet) and a post-deploy
view (one is up). Each check just reports pass/fail plus a suggested fix --
nothing here attempts to auto-fix anything.
"""

import os
import shutil
import subprocess

from canyonos import ui
from canyonos.build import AGENTS
from canyonos.constants import default_config_path, port_in_use
from canyonos.dashboard_stack import (
    REDIS_PORT,
    _container_health,
    _container_name,
    _endpoint_healthy,
    _existing_dashboard_port,
)
from canyonos.gc import deploy_status
from canyonos.init import docker_running, docker_start_command, load_state
from canyonos.verify import _runtime_table, agent_rows


def _compose_available():
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return result.returncode == 0


def _docker_daemon_fix():
    """Names the command for the active docker context, since `canyonos deploy`
    would run exactly that itself."""
    command = docker_start_command()
    if command:
        return f"run `{' '.join(command)}` -- or just run `canyonos deploy`, which starts it for you"
    return "start your Docker runtime (on Linux: `sudo systemctl start docker`)"


def _pre_deploy_checks():
    return [
        (
            "Docker installed",
            lambda: shutil.which("docker") is not None,
            "install Docker: https://docs.docker.com/get-docker/",
        ),
        (
            "Docker daemon running",
            docker_running,
            _docker_daemon_fix(),
        ),
        (
            "Docker Compose available",
            _compose_available,
            "update Docker to a version that includes Compose v2 (needed for `canyonos serve`)",
        ),
        (
            "git available",
            lambda: shutil.which("git") is not None,
            "install git (`canyonos build` fetches the porting skill with it; "
            "without git it falls back to a full-repo tarball download)",
        ),
        (
            "Coding agent available",
            lambda: any(shutil.which(spec["cli"]) for spec in AGENTS.values()),
            "install one of "
            + " or ".join(spec["label"] for spec in AGENTS.values())
            + " (`canyonos build` runs the port through it)",
        ),
    ]


def _print_check(label, ok, detail, fix):
    """Like `_run_checks`'s per-check printing, but a passing check also shows
    what it checked -- the address/container -- so the dependency graph is
    visible without extra digging, not just a bare checkmark."""
    if ok:
        ui.ok(f"{label:<24}{detail}")
    else:
        ui.fail(label)
        ui.hint(f"  -> {fix}")
    return ok


def _gc_check(state, status):
    ok = bool(status and status.get("running"))
    return _print_check(
        "Global Controller",
        ok,
        f"container {state['container_id'][:12]} on 127.0.0.1:{state['port']}",
        "GC container isn't answering -- run `canyonos deploy` again",
    )


def _redis_check():
    """TCP reachability only -- not a real PING, but enough to say something's
    listening where the local provider and dashboard both expect Redis."""
    return _print_check(
        "Redis",
        port_in_use(int(REDIS_PORT)),
        f"127.0.0.1:{REDIS_PORT}",
        "nothing is listening on 6379 -- redeploy, or check `docker ps`",
    )


def _agents_check(gc_port):
    """Prints the workflow-agent readiness table (reusing the same rows and
    table `canyonos test` shows). True if there's nothing to check or every
    agent is ready.
    """
    config_path = default_config_path()
    if not os.path.isfile(config_path):
        ui.hint(f"No config at {config_path} -- skipping workflow agent checks")
        return True

    rows = agent_rows(config_path, gc_port)
    if not rows:
        return True
    ui.panel(_runtime_table(rows))
    return all(row["ok"] for row in rows)


def _dashboard_checks():
    """Postgres/API/web checks, or one hint if the dashboard was never started
    from here -- it's an optional, separate step from the deploy itself.
    """
    port = _existing_dashboard_port()
    if port is None:
        ui.hint("Dashboard not running -- start it with `canyonos serve`")
        return True

    db_name = _container_name("db")
    db_ok = _print_check(
        "Postgres (db)",
        _container_health(db_name) == "healthy",
        db_name,
        f"the dashboard's Postgres container isn't healthy -- `canyonos serve` again, or check `docker logs {db_name}`",
    )
    api_ok = _print_check(
        "Dashboard API",
        _endpoint_healthy(f"http://127.0.0.1:{port}/api/healthz"),
        f"127.0.0.1:{port}/api/healthz",
        "the dashboard API isn't answering -- `canyonos serve` again",
    )
    web_ok = _print_check(
        "Dashboard web",
        _endpoint_healthy(f"http://127.0.0.1:{port}/healthz"),
        f"127.0.0.1:{port}/healthz",
        "the dashboard web isn't answering -- `canyonos serve` again",
    )
    return db_ok and api_ok and web_ok


def _guard(label, check):
    """Run one post-deploy check so a raised exception becomes a failed line with
    the error shown -- `canyonos doctor` must surface problems, never crash on
    them (a malformed config, an unreachable GC, a missing docker binary, ...).
    """
    try:
        return bool(check())
    except Exception as e:
        ui.fail(label)
        ui.hint(f"  -> {label} check could not run: {e}")
        return False


def _print_post_deploy(state, status):
    """Health-check every service the deploy declared, in dependency order:
    the Global Controller, Redis, each workflow agent, then the dashboard --
    one readiness table instead of `docker ps`/`docker inspect` by hand.

    Every check runs (no short-circuit) and is guarded, so one failing or
    erroring probe never hides the others or aborts the whole report.
    """
    results = [
        _guard("Global Controller", lambda: _gc_check(state, status)),
        _guard("Redis", _redis_check),
        _guard("Workflow agents", lambda: _agents_check(state["port"])),
        _guard("Dashboard", _dashboard_checks),
    ]
    return all(results)


def _run_checks(checks):
    """Run every check, print a pass/fail checklist, and return True if all passed."""
    all_ok = True
    for label, check, fix in checks:
        try:
            passed = bool(check())
        except Exception as e:
            passed = False
            fix = f"{fix} (error: {e})"

        if passed:
            ui.ok(label)
        else:
            ui.fail(label)
            ui.hint(f"  -> {fix}")
            all_ok = False

    return all_ok


def run_doctor():
    """Print the pre- or post-deploy checklist, whichever applies, and return True if it all passed."""
    try:
        state = load_state()
    except FileNotFoundError:
        state = None

    status = None
    if state:
        try:
            status = deploy_status(state["port"])
        except (KeyError, OSError, ValueError) as e:
            ui.warn(f"Could not read deploy status ({e}); assuming no deploy is running.")

    if status and status.get("running"):
        ui.say("Deploy is up -- post-deploy checks:")
        return _print_post_deploy(state, status)

    ui.say("No deploy running -- pre-deploy checks:")
    return _run_checks(_pre_deploy_checks())
