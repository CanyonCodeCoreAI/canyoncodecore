"""Shared helpers for the canyonos CLI."""

import os

import yaml
from ruamel.yaml import YAML

DEFAULT_API_PORT = 8080

# The workflow entrypoint is always exposed as POST /main with a {"query": ...}
# body, regardless of what the workflow function is called in the project.
WORKFLOW_ROUTE = "main"


def default_config_path():
    """Global controller config for the current directory, preferring the .car artifact layout."""
    car = os.path.join(".car", "config", "global_controller.yaml")
    return car if os.path.isfile(car) else os.path.join("config", "global_controller.yaml")


def workflow_api_port(config_path):
    """Host port the workflow answers on, or None if there isn't one to read."""
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None

    for agent in config.get("agents") or []:
        if agent.get("type") == "workflow":
            return agent.get("api_port", DEFAULT_API_PORT)
    return None


def workspace_relative(config_path):
    """`config_path` relative to the cwd, or None if it falls outside it.

    The container only ever receives a copy of the current directory, and it
    resolves what it's given against /workspace -- so an absolute path silently
    discards that prefix and a `../` one escapes it. Both then 404 naming a
    path that exists on the host, which reads as a bug in the wrong place.
    """
    # realpath on both sides: a symlinked project dir (or macOS's /tmp ->
    # /private/tmp) otherwise makes an in-project absolute path look external.
    relative = os.path.relpath(os.path.realpath(config_path), os.path.realpath(os.getcwd()))
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        return None
    return relative


def round_trip_yaml():
    """Loader that preserves comments, key order, quoting and ${ENV} refs.

    The indent settings match the project's YAML style, so edits don't reflow
    list indentation: block sequences stay indented under their key.
    """
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    return yaml_rt
