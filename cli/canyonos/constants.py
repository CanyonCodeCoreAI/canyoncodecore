"""Shared helpers for the canyonos CLI.

Config/data layer. Holds shared values and parsing helpers"""

import ast
import os
import socket

import yaml
from ruamel.yaml import YAML

DEFAULT_API_PORT = 8080
DEFAULT_DASHBOARD_PORT = 8081

# Fallback when the real function name/params can't be determined statically
# (see workflow_entrypoint) -- canyonos_core's own examples all follow this shape.
WORKFLOW_ROUTE = "main"
DEFAULT_QUERY_PARAM = "query"


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


def _source_root(config_path):
    """Directory `workflow_file` is relative to -- `.car/app` under the .car layout, else the project root."""
    car_root = os.path.dirname(os.path.dirname(config_path)) or "."
    return os.path.join(car_root, "app") if os.path.basename(car_root) == ".car" else car_root


def _deploy_call_target(tree):
    """The name passed as `deploy(<name>, ...)`'s first argument, or None."""
    for node in ast.walk(tree):
        is_deploy_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) == "deploy"
        )
        if is_deploy_call and node.args and isinstance(node.args[0], ast.Name):
            return node.args[0].id
    return None


def workflow_entrypoint(config_path):
    """(route, [(param_name, example_default_or_None), ...]) read statically from the
    workflow's own source -- the function `deploy()` is actually called with, not an
    assumed name. Returns None if the file, the deploy() call, or the function can't be found."""
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None

    workflow_file = next(
        (a.get("workflow_file") for a in config.get("agents") or [] if a.get("type") == "workflow"),
        None,
    )
    if not workflow_file:
        return None

    workflow_path = os.path.join(_source_root(config_path), workflow_file)
    try:
        with open(workflow_path) as f:
            tree = ast.parse(f.read(), filename=workflow_path)
    except (OSError, SyntaxError):
        return None

    fn_name = _deploy_call_target(tree)
    if fn_name is None:
        return None

    fn_def = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name),
        None,
    )
    if fn_def is None:
        return None

    args = [a.arg for a in fn_def.args.args if a.arg != "self"]
    defaults = fn_def.args.defaults
    first_defaulted = len(args) - len(defaults)
    params = []
    for i, name in enumerate(args):
        default = None
        if i >= first_defaulted:
            try:
                default = ast.literal_eval(defaults[i - first_defaulted])
            except (ValueError, TypeError):
                default = None
        params.append((name, default))
    return fn_name, params


def port_in_use(port):
    """True if something is listening on this host port already."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def dashboard_port(config_path):
    """Host port the local dashboard prefers to start on, falling back to the default."""
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_DASHBOARD_PORT

    for agent in config.get("agents") or []:
        if agent.get("type") == "workflow":
            return agent.get("dashboard_port", DEFAULT_DASHBOARD_PORT)
    return DEFAULT_DASHBOARD_PORT


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
