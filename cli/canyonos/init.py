"""
Logic for `canyonos init`, does the following:
1. Pull the Global Controller image
2. Start a container from it
3. Record where it's listening so cli knows where to send requests.
"""

import json
import os
import subprocess

# Image Name, need to switch to CanyonCore Organization Namespace later
GC_IMAGE = "saakeths/canyonos:latest"
GC_CONTAINER_PORT = 8000

STATE_DIR = os.path.expanduser("~/.canyonos")
STATE_PATH = os.path.join(STATE_DIR, "state.json")


def pull_image(image=GC_IMAGE):
    subprocess.run(["docker", "pull", image], check=True)


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
            return result.stdout.strip(), port
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
    pull_image()
    container_id, port = run_container()
    save_state(container_id, port)
    print(f"Global Controller running in container {container_id[:12]} on port {port}")
