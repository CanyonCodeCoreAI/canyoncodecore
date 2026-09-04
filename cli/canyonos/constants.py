"""Shared constants for the canyonos CLI."""

import os


def default_config_path():
    """Global controller config for the current directory, preferring the .car artifact layout."""
    car = os.path.join(".car", "config", "global_controller.yaml")
    return car if os.path.isfile(car) else os.path.join("config", "global_controller.yaml")
