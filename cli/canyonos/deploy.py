"""
Logic for `canyonos deploy`: tell the Global Controller container to deploy
the project at the current working directory (bind-mounted at /runtime by
`canyonos init`).
"""

import json
import urllib.error
import urllib.request

from canyonos.build import DEFAULT_CONFIG_PATH
from canyonos.init import load_state


def run_deploy(config_path=DEFAULT_CONFIG_PATH):
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return

    url = f"http://127.0.0.1:{state['port']}/deploy"
    body = json.dumps({"config_path": config_path}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            print(f"Deployed: pid {data.get('pid')}")
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        print(f"Deploy failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
