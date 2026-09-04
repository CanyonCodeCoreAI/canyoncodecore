"""
Logic for `canyonos deploy`: copy the project into the container's /workspace
volume (via `canyonos sync`), then tell the Global Controller container to
build and deploy it. The container's `ventis deploy` handles both the build
(stubs, protos, Docker images) and the launch -- the CLI just ships files,
triggers it, and streams the logs.

Once the deploy's logs report the workflow is actually up, `canyonos serve`
is kicked off automatically so the local dashboard is ready without an extra
manual step.
"""

import json
import subprocess
import urllib.error
import urllib.request

from canyonos.constants import default_config_path
from canyonos.init import load_state, run_init
from canyonos.serve import run_serve
from canyonos.sync import run_sync

# Logged exactly once by GlobalController.run(), right after `_wait_for_healthy()`
# returns -- the signal that the workflow finished coming up and entered its
# steady-state polling loop.
_WORKFLOW_UP_MARKER = "Global controller started, polling every"


def run_deploy(config_path=None, serve=True):
    config_path = config_path or default_config_path()
    run_init()

    # Copy the current project into the container before building/deploying.
    if not run_sync():
        return

    state = load_state()

    url = f"http://127.0.0.1:{state['port']}/deploy"
    body = json.dumps({"config_path": config_path}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
            _stream_logs_and_autoserve(state["container_id"], serve=serve)
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        print(f"Deploy failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")


def _stream_logs_and_autoserve(container_id, serve=True):
    """Tail the GC container's logs (same as before), and -- unless disabled
    via `serve=False` -- launch `canyonos serve` the moment they show the
    workflow is up, so the dashboard is ready alongside it. Log tailing
    continues afterwards exactly as before.
    """
    process = subprocess.Popen(
        ["docker", "logs", "-f", container_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    served = not serve
    try:
        for line in process.stdout:
            print(line, end="")
            if not served and _WORKFLOW_UP_MARKER in line:
                served = True
                print("\nWorkflow is up -- starting the local dashboard (canyonos serve)...")
                try:
                    run_serve()
                except Exception as e:
                    print(f"Could not start the dashboard automatically: {e}")
                    print("Run `canyonos serve` manually to view it.")
    except KeyboardInterrupt:
        print("\nStopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
        print("To resubscribe to log stream run `canyonos logs`.")
    finally:
        if process.poll() is None:
            process.terminate()
