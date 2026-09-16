"""
Logic for `canyonos quit`: full teardown. Stops and removes the Global
Controller container AND deletes the /workspace named volume, so the project
files copied into it are discarded too. Also tears down the local dashboard
stack (web/api/db), if one was started from this project. (Use `canyonos stop`
to only halt a running deploy while keeping the containers and files around.)
"""

import os

from canyonos import ui
from canyonos.docker_cmd import (
    DOCKER_CLEANUP_TIMEOUT,
    DOCKER_QUICK_TIMEOUT,
    cleanup_docker,
    run_docker,
)
from canyonos.dashboard_stack import teardown_dashboard
from canyonos.gc import GCError, post_clean, require_state
from canyonos.init import GC_WORKSPACE_VOLUME, STATE_PATH


def _container_state(container_id):
    """(exists, running), as far as Docker will say.

    An inspect Docker can't answer at all reads as a container that is still
    there and still up, so teardown attempts every removal instead of skipping
    to deleting the state that records what to remove.
    """
    try:
        result = run_docker(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            timeout=DOCKER_QUICK_TIMEOUT,
            action=f"Inspecting Docker container {container_id[:12]}",
        )
    except RuntimeError:
        return True, True
    if result.returncode != 0:
        return False, False
    return True, result.stdout.strip() == "true"


def run_quit():
    """Tear down the container, volume and dashboard; returns what it couldn't.

    Teardown reports rather than raises. It runs both on its own and as the
    first step of `canyonos deploy`, so a resource that won't go away must not
    also block starting over -- the state file is preserved either way, which
    is what makes a later retry possible.
    """
    state = require_state()
    if state is None:
        return []

    container_id = state["container_id"]
    with ui.status("Tearing down..."):
        failures = []
        # A stale record cannot answer /clean, so establish this before making
        # an HTTP request that would otherwise add a full request timeout.
        exists, container_running = _container_state(container_id)

        # Stop any running deploy first, so the local controller and Redis
        # containers it spawned via docker-outside-of-docker get torn down
        # too. Removing the GC container itself doesn't touch them -- they're
        # sibling containers on the host, not nested inside it.
        if container_running:
            try:
                post_clean(state["port"])
            except GCError as e:
                # 409 is "no deploy running", which is the desired end state.
                if e.code != 409:
                    failures.append(f"Cleaning the running deployment failed: {e}")

            failure = cleanup_docker(
                ["docker", "stop", container_id],
                action=f"Stopping Global Controller {container_id[:12]}",
                missing_text="No such container",
                timeout=DOCKER_CLEANUP_TIMEOUT,
            )
            if failure:
                failures.append(failure)

        if exists:
            failure = cleanup_docker(
                ["docker", "rm", container_id],
                action=f"Removing Global Controller {container_id[:12]}",
                missing_text="No such container",
                timeout=DOCKER_CLEANUP_TIMEOUT,
            )
            if failure:
                failures.append(failure)

        # Remove the workspace volume only after the container is gone (docker
        # refuses to remove a volume still in use). A missing volume is already
        # the desired end state; every other failure remains retryable.
        failure = cleanup_docker(
            ["docker", "volume", "rm", GC_WORKSPACE_VOLUME],
            action=f"Removing workspace volume {GC_WORKSPACE_VOLUME}",
            missing_text="No such volume",
            timeout=DOCKER_CLEANUP_TIMEOUT,
        )
        if failure:
            failures.append(failure)
        if not teardown_dashboard():
            failures.append("Removing the dashboard stack failed")

        if not failures:
            try:
                os.remove(STATE_PATH)
            except FileNotFoundError:
                pass
            except OSError as e:
                failures.append(f"Removing CanyonOS state at {STATE_PATH} failed: {e}")

    if failures:
        ui.fail("Teardown incomplete:\n- " + "\n- ".join(failures))
        ui.hint(f"State was preserved at {STATE_PATH}, so `canyonos quit` can be retried.")
    elif not exists:
        ui.warn(f"Global Controller container {container_id[:12]} was already gone; cleaned up local state.")
    else:
        ui.ok(f"Global Controller container {container_id[:12]} torn down (volume removed)")
    return failures
