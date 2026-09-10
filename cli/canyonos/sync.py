"""
Logic for `canyonos sync`: copy the current project directory into the Global
Controller container's /workspace volume via `docker cp`.

Files live inside the container's named volume (see `init.py`), not on a live
bind mount, so host-side edits don't reach a running build. `docker cp` is
additive -- it overwrites and adds but never deletes -- so a standalone
re-sync leaves behind anything removed from the host since the last one.
That can't accumulate across deploys: `canyonos deploy` quits any previous
controller first, which removes the volume.
"""

import os
import subprocess

from canyonos import ui
from canyonos.gc import require_state
from canyonos.init import GC_WORKSPACE_PATH


def run_sync():
    """Copy the current directory into the container. Returns True on success."""
    state = require_state()
    if state is None:
        return False

    container_id = state["container_id"]
    # Trailing "/." copies the *contents* of the current directory into
    # /workspace, rather than nesting it under /workspace/<dirname>.
    src = os.path.join(os.getcwd(), ".")
    label = f"Syncing {os.getcwd()} -> {container_id[:12]}:{GC_WORKSPACE_PATH}"

    with ui.status(f"{label}..."):
        # Captured so docker's own progress output doesn't clobber the spinner.
        result = subprocess.run(
            ["docker", "cp", src, f"{container_id}:{GC_WORKSPACE_PATH}"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        ui.fail(f"Sync failed: {result.stderr.strip() or result.stdout.strip()}")
        return False

    ui.ok("Sync complete.")
    return True
