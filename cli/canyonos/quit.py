"""
Logic for `canyonos quit`: full teardown. Stops and removes the Global
Controller container AND deletes the /workspace named volume, so the project
files copied into it are discarded too. (Use `canyonos stop` to only halt a
running deploy while keeping the container and files around.)
"""

import os
import subprocess

from rich.console import Console

from canyonos.init import GC_WORKSPACE_VOLUME, STATE_PATH, load_state


def run_quit():
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running.")
        return

    container_id = state["container_id"]
    console = Console()
    with console.status("Tearing down..."):
        subprocess.run(["docker", "stop", container_id], check=True)
        subprocess.run(["docker", "rm", container_id], check=True)

        # Remove the workspace volume only after the container is gone (docker
        # refuses to remove a volume still in use). check=False so a missing
        # volume doesn't turn teardown into an error.
        subprocess.run(["docker", "volume", "rm", GC_WORKSPACE_VOLUME], check=False)
        os.remove(STATE_PATH)

    print(f"Global Controller container {container_id[:12]} torn down (volume removed)")
