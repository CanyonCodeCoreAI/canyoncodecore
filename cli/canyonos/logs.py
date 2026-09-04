"""
Logic for `canyonos logs`: re-subscribe to the running deploy's log stream.
"""

import json
import subprocess
import urllib.error
import urllib.request

from canyonos.init import load_state


def run_logs():
    try:
        state = load_state()
    except FileNotFoundError:
        print("No Global Controller container is running. Run `canyonos init` first.")
        return

    url = f"http://127.0.0.1:{state['port']}/status"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Could not reach Global Controller container: {e}")
        return

    if not data.get("running"):
        print("No deploy running, run `canyonos deploy` to deploy project.")
        return

    try:
        subprocess.run(["docker", "logs", "-f", state["container_id"]])
    except KeyboardInterrupt:
        print("\nStopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
