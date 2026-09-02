"""
Logic for `canyonos init`, does the following:
1. Pull the Global Controller image
2. Start a container from it
3. Record where it's listening so cli knows where to send requests.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

# Formatting
from pyfiglet import figlet_format
from rich.console import Console



# Image Name, need to switch to CanyonCore Organization Namespace later
GC_IMAGE = "saakeths/canyonos:latest"
GC_CONTAINER_PORT = 8000

STATE_DIR = os.path.expanduser("~/.canyonos")
STATE_PATH = os.path.join(STATE_DIR, "state.json")


def pull_image(image=GC_IMAGE):
    subprocess.run(["docker", "pull", image], check=True)


def _port_reachable(port, attempts=10, delay=0.5):
    """
    A successful `docker run` only means Docker accepted the port binding --
    not that traffic actually flows. OrbStack's own port-forwarding proxy for
    a given port can get stuck (heavy churn on the same port is enough to
    trigger it), which looks fine at the Docker level but resets every real
    connection. Confirm the container is actually reachable before trusting it.
    """
    import time

    url = f"http://127.0.0.1:{port}/status"
    for _ in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(delay)
    return False


def run_container(image=GC_IMAGE, max_attempts=50):
    port = GC_CONTAINER_PORT
    for _ in range(max_attempts):
        result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-p",
                f"127.0.0.1:{port}:{GC_CONTAINER_PORT}",
                # Docker-outside-of-Docker: GC shells out to `docker` to launch
                # Redis/agent containers, so it needs the host's real daemon,
                # not a nested one.
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "-v",
                f"{os.getcwd()}:/runtime",
                "--add-host=host.docker.internal:host-gateway",
                "-e",
                "VENTIS_REDIS_HOST=host.docker.internal",
                image,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            container_id = result.stdout.strip()
            if _port_reachable(port):
                return container_id, port
            # Port bound fine but never actually became reachable -- treat
            # like a conflict, since that's effectively what it is.
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
            port += 1
            continue
        if "port is already allocated" in result.stderr:
            port += 1
            continue
        raise RuntimeError(result.stderr)
    raise RuntimeError(f"no free port found after {max_attempts} attempts starting at {GC_CONTAINER_PORT}")


def save_state(container_id, port):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump({"container_id": container_id, "port": port}, f)


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def run_init():
    console = Console()
    banner = figlet_format("CANYON OS", font="ansi_shadow", width=200)
    
    colors = [
        "#2BD17E",
        "#55DA98",
        "#80E3B2",
        "#AAEDCB",
        "#D5F6E5",
        "#FFFFFF",
    ]
    
    for line, color in zip(banner.splitlines(), colors):
        console.print(line, style=color)


    pull_image()
    container_id, port = run_container()
    save_state(container_id, port)
    print(f"Global Controller running in container {container_id[:12]} on port {port}")
