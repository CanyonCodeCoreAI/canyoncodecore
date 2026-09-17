"""Logic for canyonos clean: remove generated artifacts and Docker images."""

import os
import shutil
import subprocess

from canyonos import ui

# Core tags every image it builds "canyonos-<agent>"; the ghcr.io dashboard
# images must not match.
IMAGE_REFERENCE = "canyonos-*"


def _remove_canyon_images():
    try:
        result = subprocess.run(
            ["docker", "images", "--filter", f"reference={IMAGE_REFERENCE}", "-q"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        ui.warn(f"Could not clean Docker images: {error}")
        return

    if result.returncode != 0:
        detail = result.stderr.strip() or "docker images failed"
        ui.warn(f"Could not list Canyon Docker images: {detail}")
        return

    image_ids = list(dict.fromkeys(result.stdout.split()))
    if not image_ids:
        return

    try:
        with ui.status("Removing Canyon Docker images..."):
            result = subprocess.run(
                ["docker", "image", "rm", *image_ids],
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as error:
        ui.warn(f"Could not clean Docker images: {error}")
        return

    if result.returncode != 0:
        detail = result.stderr.strip() or "docker image rm failed"
        ui.warn(f"Could not remove Canyon Docker images: {detail}")
        return

    ui.ok(f"Removed {len(image_ids)} Canyon Docker image(s).")


def run_clean():
    car_dir = os.path.join(os.getcwd(), ".car")

    if os.path.isdir(car_dir):
        with ui.status(f"Cleaning {car_dir}..."):
            shutil.rmtree(car_dir)
        ui.ok("Clean complete.")
    else:
        ui.warn("Nothing to clean, no .car folder in root")

    _remove_canyon_images()
