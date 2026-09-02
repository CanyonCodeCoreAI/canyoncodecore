"""
Logic for `canyonos stop`: stop the running deploy inside the Global
Controller container (SIGTERM, same teardown as Ctrl+C would trigger).
"""

import json
import urllib.error
import urllib.request

from rich.console import Console

from canyonos.init import load_state


def run_stop():
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return

    url = f"http://127.0.0.1:{state['port']}/clean"
    req = urllib.request.Request(url, method="POST")

    console = Console()
    try:
        with console.status("Stopping deploy..."):
            with urllib.request.urlopen(req) as resp:
                json.loads(resp.read())
        print("Deploy stopped.")
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        print(f"Stop failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
