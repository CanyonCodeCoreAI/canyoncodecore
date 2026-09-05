"""
Logic for `canyonos init`, does the following:
1. Pull the Global Controller image
2. Start a container from it
3. Record where it's listening so cli knows where to send requests.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Formatting
from pyfiglet import figlet_format

from canyonos import ui



# Image Name, need to switch to CanyonCore Organization Namespace later
GC_IMAGE = "saakeths/canyonos:latest"
GC_CONTAINER_PORT = 8000

# Named docker volume mounted at /workspace inside the container. Files are
# copied in via `canyonos sync` (docker cp), not mounted live, so host-side
# edits don't reach a running build. `canyonos quit` removes the volume, and
# since every deploy quits any previous controller first, each deploy starts
# from an empty workspace.
GC_WORKSPACE_VOLUME = "canyonos-workspace"
GC_WORKSPACE_PATH = "/workspace"

STATE_DIR = os.path.expanduser("~/.canyonos")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

# How to start the daemon behind each docker context, as (CLI command, macOS
# app). Keyed off the *active context* rather than which app is installed: with
# both Docker Desktop and OrbStack present, guessing by app bundle starts the
# wrong daemon and then waits out the timeout against a socket nothing is
# listening on.
DOCKER_RUNTIMES = {
    "orbstack": (["orb", "start"], "OrbStack"),
    "colima": (["colima", "start"], None),
    "desktop-linux": (None, "Docker"),
    "default": (None, "Docker"),
}
DOCKER_START_TIMEOUT = 60


def docker_running():
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except OSError:
        return False


def docker_start_command():
    """The command that starts the daemon for the active context, or None."""
    try:
        result = subprocess.run(
            ["docker", "context", "show"], capture_output=True, text=True
        )
    except OSError:
        return None

    context = result.stdout.strip() if result.returncode == 0 else "default"
    command, app = DOCKER_RUNTIMES.get(context, (None, "Docker"))
    if command and shutil.which(command[0]):
        return command
    if app and sys.platform == "darwin" and os.path.isdir(f"/Applications/{app}.app"):
        return ["open", "-a", app]
    return None


def ensure_docker_running(timeout=DOCKER_START_TIMEOUT):
    if docker_running():
        return

    command = docker_start_command()
    if command is None:
        # Linux/systemd wants root here; escalating on the user's behalf is not
        # this CLI's call to make.
        raise RuntimeError(
            "Docker isn't running, and there's no way to start it for the current "
            "docker context. Start it (on Linux: `sudo systemctl start docker`) and re-run."
        )

    ui.say(f"Docker isn't running -- starting it with `{' '.join(command)}`...")
    subprocess.run(command, capture_output=True)

    deadline = time.time() + timeout
    with ui.status("Waiting for the Docker daemon..."):
        while time.time() < deadline:
            if docker_running():
                ui.ok("Docker is running.")
                return
            time.sleep(1)

    raise RuntimeError(
        f"Docker did not become ready within {timeout}s. Start it manually and re-run."
    )


def pull_image(image=GC_IMAGE):
    # Capture output so the rich status spinner isn't clobbered by docker's own
    # layer-progress printing -- but surface it on failure (auth, network,
    # rate-limit, missing arch, etc. all otherwise look like the same opaque
    # "exit status 1").
    result = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker pull {image} failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def _port_reachable(port, attempts=10, delay=0.5):
    """
    A successful `docker run` only means Docker accepted the port binding --
    not that traffic actually flows. OrbStack's own port-forwarding proxy for
    a given port can get stuck (heavy churn on the same port is enough to
    trigger it), which looks fine at the Docker level but resets every real
    connection. Confirm the container is actually reachable before trusting it.
    """
    url = f"http://127.0.0.1:{port}/status"
    for _ in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(delay)
    return False


def run_container(image=GC_IMAGE, max_attempts=50, extra_env=None):
    port = GC_CONTAINER_PORT
    for _ in range(max_attempts):
        cmd = [
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
            f"{GC_WORKSPACE_VOLUME}:{GC_WORKSPACE_PATH}",
            "--add-host=host.docker.internal:host-gateway",
            "-e",
            "CANYONOS_REDIS_HOST=host.docker.internal",
        ]
        # Extra env for the GC container. The local runtime forwards select keys
        # (e.g. CANYONOS_LLM_STUB_TEXT) from here into each agent container.
        for _k, _v in (extra_env or {}).items():
            cmd.extend(["-e", f"{_k}={_v}"])
        cmd.append(image)  # image must come after all flags
        result = subprocess.run(cmd, capture_output=True, text=True)
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


def quit_existing():
    """Tear down a previously started Global Controller, if state records one.

    Without this each run starts another container on the next free port and
    orphans the last one, which then can't be reached through state.json.
    """
    # Deferred: quit.py imports from this module, so a top-level import cycles.
    from canyonos.quit import run_quit

    if os.path.isfile(STATE_PATH):
        run_quit()


def run_init(banner=True, extra_env=None):
    if banner:
        ui.gradient(figlet_format("CANYON OS", font="ansi_shadow", width=200))

    # Before quit_existing(), which shells out to docker itself.
    ensure_docker_running()
    quit_existing()

    with ui.status("Pulling Global Controller image..."):
        pull_image()
    with ui.status("Starting Global Controller container..."):
        container_id, port = run_container(extra_env=extra_env)
    save_state(container_id, port)
    ui.ok(f"Global Controller running in container {container_id[:12]} on port {port}")
