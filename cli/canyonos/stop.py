"""
Logic for `canyonos stop`: stop the running deploy inside the Global
Controller container (SIGTERM, same teardown as Ctrl+C would trigger).
"""

from canyonos import ui
from canyonos.gc import GCError, post_clean, require_state


def run_stop():
    state = require_state()
    if state is None:
        return

    try:
        with ui.status("Stopping deploy..."):
            post_clean(state["port"])
        ui.ok("Deploy stopped.")
    except GCError as e:
        ui.fail(e)
