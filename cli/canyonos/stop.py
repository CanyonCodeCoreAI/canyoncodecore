"""
Logic for `canyonos stop`: stop the running deploy inside the Global
Controller container (SIGTERM, same teardown as Ctrl+C would trigger).
"""

import json
import urllib.error
import urllib.request

from rich.console import Console

from canyonos.init import load_state


def _post_clean(port):
    """POST /clean to the Global Controller container.

    This is what actually tears down the local controller and Redis
    containers a deploy spawned via docker-outside-of-docker: it sends
    SIGTERM to the in-container `ventis deploy` process, whose handler calls
    `GlobalController.stop()` and blocks until it returns. Shared with
    `canyonos quit`, which needs the same teardown before removing the GC
    container itself.
    """
    url = f"http://127.0.0.1:{port}/clean"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def run_stop():
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return

    console = Console()
    try:
        with console.status("Stopping deploy..."):
            _post_clean(state["port"])
        print("Deploy stopped.")
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
        print(f"Stop failed: {data.get('error')}")
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
