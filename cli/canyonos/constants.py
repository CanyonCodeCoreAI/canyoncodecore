"""Shared constants for the canyonos CLI."""

# Default path (relative to the application root) to the global controller
# config. Canyon-owned files live under `.car`, so this mirrors
# `ventis.cli.DEFAULT_CONFIG_PATH`; kept literal because this CLI ships
# separately from the ventis package and cannot import it.
DEFAULT_CONFIG_PATH = ".car/config/global_controller.yaml"
