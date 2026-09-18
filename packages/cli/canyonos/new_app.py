"""
Logic for `canyonos new-app`: scaffold a new project in the current
directory. Runs locally, no container involved.
"""

import os

from canyonos import ui


def run_new_app():
    if os.listdir("."):
        ui.fail("Directory is not empty. Run `canyonos new-app` in an empty directory.")
        return

    for folder in ("agents", "config", "workflow"):
        os.makedirs(folder)
    open(".env", "w").close()

    for filename in ("global_controller.yaml", "policy.yaml"):
        open(os.path.join("config", filename), "w").close()

    ui.ok("Created new CanyonOS project.")
