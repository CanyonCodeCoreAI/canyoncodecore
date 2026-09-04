"""
Logic for `canyonos deploy`: copy the project into the container's /workspace
volume (via `canyonos sync`), then tell the Global Controller container to
build and deploy it. The container's `ventis deploy` handles both the build
(stubs, protos, Docker images) and the launch -- the CLI just ships files,
triggers it, and streams the logs.
"""

import json
import subprocess
import urllib.error
import urllib.request

from canyonos.constants import DEFAULT_CONFIG_PATH
from canyonos.init import load_state, run_init
from canyonos.sync import run_sync


def run_deploy(config_path=DEFAULT_CONFIG_PATH):
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
            try:
                subprocess.run(["docker", "logs", "-f", state["container_id"]])
            except KeyboardInterrupt:
                print("\nStopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
                print("To resubscribe to log stream run `canyonos logs`.")
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        print(f"Deploy failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
