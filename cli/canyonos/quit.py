"""
Stops the CanyonOS Container that is running
"""

import os
import subprocess

from canyonos.init import STATE_PATH, load_state


def run_quit():
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running.")
        return

    container_id = state["container_id"]
    subprocess.run(["docker", "stop", container_id], check=True)
    subprocess.run(["docker", "rm", container_id], check=True)
    os.remove(STATE_PATH)

    print(f"Global Controller container {container_id[:12]} stopped")
