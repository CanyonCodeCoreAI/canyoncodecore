"""
Logic for `canyonos build`: copy the project into the container's /workspace
volume (via `canyonos sync`), then tell the Global Controller container to
build it. The build itself (stub generation, proto compilation, Docker context
generation and image builds) runs inside the container where ventis is
installed -- the CLI only ships the files, kicks it off, and prints the result.
"""

import json
import urllib.error
import urllib.request

from canyonos.constants import DEFAULT_CONFIG_PATH
from canyonos.init import load_state
from canyonos.sync import run_sync


def run_build(config_path=DEFAULT_CONFIG_PATH):
    # Copy the current project into the container before building it.
    if not run_sync():
        return

    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return

    url = f"http://127.0.0.1:{state['port']}/build"
    body = json.dumps({"config_path": config_path}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            if data.get("output"):
                print(data["output"], end="")
            print("Build complete.")
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        if data.get("output"):
            print(data["output"], end="")
        print(f"Build failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
