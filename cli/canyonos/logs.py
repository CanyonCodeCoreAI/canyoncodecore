"""
Logic for `canyonos logs`: re-subscribe to the running deploy's log stream.
"""

import subprocess

from canyonos import ui
from canyonos.gc import deploy_status, require_state


def run_logs():
    state = require_state()
    if state is None:
        return

    status = deploy_status(state["port"])
    if status is None:
        ui.fail("Could not reach Global Controller container.")
        return

    if not status.get("running"):
        ui.warn("No deploy running, run `canyonos deploy` to deploy project.")
        return

    try:
        subprocess.run(["docker", "logs", "-f", state["container_id"]])
    except KeyboardInterrupt:
        ui.blank()
        ui.say("Stopped monitoring log stream. Run `canyonos stop` to stop the deploy.")
