"""
Logic for `canyonos clean`: remove the generated .car artifact directory.
"""

import os
import shutil

from canyonos import ui


def run_clean():
    car_dir = os.path.join(os.getcwd(), ".car")

    if not os.path.isdir(car_dir):
        ui.warn("Nothing to clean, no .car folder in root")
        return

    with ui.status(f"Cleaning {car_dir}..."):
        shutil.rmtree(car_dir)
    ui.ok("Clean complete.")
