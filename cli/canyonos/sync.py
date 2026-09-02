"""
Logic for `canyonos sync`: copy the current project directory into the Global
Controller container's /workspace volume via `docker cp`.

Files live inside the container's named volume (see `init.py`), not on a live
bind mount -- so they persist across `canyonos quit` and survive host-side
changes. `docker cp` is additive: it overwrites/adds files but never deletes,
so build outputs generated inside the container (stubs/, grpc_stubs/,
docker_container/) survive a re-sync of the host source.
"""

import os
import subprocess

from canyonos.init import GC_WORKSPACE_PATH, load_state


def run_sync():
    """Copy the current directory into the container. Returns True on success."""
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return False

    container_id = state["container_id"]
    # Trailing "/." copies the *contents* of the current directory into
    # /workspace, rather than nesting it under /workspace/<dirname>.
    src = os.path.join(os.getcwd(), ".")
    print(f"Syncing {os.getcwd()} -> {container_id[:12]}:{GC_WORKSPACE_PATH} ...")

    result = subprocess.run(
        ["docker", "cp", src, f"{container_id}:{GC_WORKSPACE_PATH}"]
    )
    if result.returncode != 0:
        print("Sync failed.")
        return False

    print("Sync complete.")
    return True
