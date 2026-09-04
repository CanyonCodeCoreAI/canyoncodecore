"""
Logic for `canyonos quit`: full teardown. Stops and removes the Global
Controller container AND deletes the /workspace named volume, so the project
files copied into it are discarded too. (Use `canyonos stop` to only halt a
running deploy while keeping the container and files around.)
"""

import os
import subprocess

from rich.console import Console

from canyonos.gc import GCError, post_clean, require_state
from canyonos.init import GC_WORKSPACE_VOLUME, STATE_PATH


def _container_exists(container_id):
    result = subprocess.run(
        ["docker", "inspect", container_id], capture_output=True
    )
    return result.returncode == 0


def run_quit():
    state = require_state()
    if state is None:
        return

    container_id = state["container_id"]
    console = Console()
    with console.status("Tearing down..."):
        # Stop any running deploy first, so the local controller and Redis
        # containers it spawned via docker-outside-of-docker get torn down
        # too. Removing the GC container itself doesn't touch them -- they're
        # sibling containers on the host, not nested inside it.
        try:
            post_clean(state["port"])
        except GCError:
            # Nothing was running, or the GC is already unreachable/gone.
            pass

        # state.json can go stale (daemon restarted, container removed by
        # hand, a previous `quit` died partway through) -- don't let a
        # missing container turn `quit` into a crash instead of a cleanup.
        already_gone = not _container_exists(container_id)
        if not already_gone:
            subprocess.run(["docker", "stop", container_id], check=False, capture_output=True)
            subprocess.run(["docker", "rm", container_id], check=False, capture_output=True)

        # Remove the workspace volume only after the container is gone (docker
        # refuses to remove a volume still in use). check=False so a missing
        # volume doesn't turn teardown into an error.
        subprocess.run(["docker", "volume", "rm", GC_WORKSPACE_VOLUME], check=False, capture_output=True)
        os.remove(STATE_PATH)

    if already_gone:
        print(f"Global Controller container {container_id[:12]} was already gone; cleaned up local state.")
    else:
        print(f"Global Controller container {container_id[:12]} torn down (volume removed)")
