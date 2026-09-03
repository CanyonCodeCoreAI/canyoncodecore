"""
Logic for `canyonos new-app`: scaffold a new project in the current
directory. Runs locally, no container involved.
"""

import os


def run_new_app():
    if os.listdir("."):
        print("Directory is not empty. Run `canyonos new-app` in an empty directory.")
        return

    for folder in ("agents", "config", "workflow"):
        os.makedirs(folder)
    open(".env", "w").close()

    for filename in ("global_controller.yaml", "policy.yaml"):
        open(os.path.join("config", filename), "w").close()

    print("Created new CanyonOS project.")
