"""
Logic for `canyonos stop`: stop the running deploy inside the Global
Controller container (SIGTERM, same teardown as Ctrl+C would trigger).
"""

from rich.console import Console

from canyonos.gc import GCError, post_clean, require_state


def run_stop():
    state = require_state()
    if state is None:
        return

    console = Console()
    try:
        with console.status("Stopping deploy..."):
            post_clean(state["port"])
        print("Deploy stopped.")
    except GCError as e:
        print(e)
