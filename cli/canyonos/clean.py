"""
Logic for `canyonos clean`: remove the generated .car artifact directory.
"""

import os
import shutil


def run_clean():
    car_dir = os.path.join(os.getcwd(), ".car")

    if not os.path.isdir(car_dir):
        print("Nothing to clean, no .car folder in root")
        return

    print(f"Cleaning {car_dir}...")
    shutil.rmtree(car_dir)
    print("Clean complete.")
