#!/usr/bin/env python3
"""Deterministic checks for a Ventis port.

Every rule in SKILL.md marked MUST or NEVER that a machine can decide is decided
here. Nothing in this file imports the port -- YAML is parsed, Python is parsed
to an AST, and neither is executed. A port that fails here fails at build, at
deploy, or on its first request; `ventis build` will not tell you, because it
never imports your agent, and a replica will not tell you either, because the
controller writes `healthy` to Redis before `_load_agent` runs.

    python validate.py [project_dir] [-c config/global_controller.yaml]
                       [--json] [--strict]

Exit 1 if any ERROR was reported, 0 otherwise. --strict also fails on warnings.

Some rules depend on Ventis features that are not on `main`. Rather than assume,
this script probes the importable `ventis` package and reports each capability
with the PR that carries it. A check whose capability is absent is reported as
UNAVAILABLE, never silently skipped.
"""

import argparse
import ast
import builtins
import json
import os
import re
import subprocess
import sys
from typing import ClassVar

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a Ventis dependency
    sys.stderr.write("validate.py needs pyyaml: pip install pyyaml\n")
    raise SystemExit(2) from None


DEFAULT_CONFIG_PATH = "config/global_controller.yaml"

# Copied flat into every image over the swept project tree, so a project module
# landing flat under one of these names is overwritten.
# ventis/stub_generator.py generate_docker / generate_workflow_docker.
RUNTIME_FLAT_NAMES = frozenset(
    {
        "future.py",
        "ventis_context.py",
        "local_controller.py",
        "local_controller_frontend.py",
        "redis_client.py",
        "grpc_options.py",
        "gpu_metrics.py",
        "bedrock.py",
        "deploy.py",
        "session_logging.py",
        "workflow_launcher.py",
    }
)

# ventis/stub_generator.py BASE_AGENT_REQUIREMENTS / BASE_WORKFLOW_REQUIREMENTS.
BASE_AGENT_REQUIREMENTS = [
    "grpcio",
    "grpcio-tools",
    "redis",
    "pyyaml",
    "psutil",
    "ipdb",
    "ipython",
    "boto3",
]
BASE_WORKFLOW_REQUIREMENTS = BASE_AGENT_REQUIREMENTS + [
    "flask",
    "sqlalchemy",
    "psycopg[binary]",
]

# ventis/cli.py EC2_REQUIRED_CONFIG_KEYS is shorter than what EC2/_runtime.py
# actually demands; the CLI preflight passes and provisioning then fails.
EC2_REQUIRED_CONFIG_KEYS = (
    "ami_id",
    "subnet_id",
    "security_group_ids",
    "region",
    "ssh_user",
)

# Import name -> distribution name, for the handful where they differ and the
# mismatch would otherwise be reported as a missing requirement.
IMPORT_TO_DISTRIBUTION = {
    "attr": "attrs",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "grpc": "grpcio",
    "grpc_tools": "grpcio-tools",
    "jwt": "pyjwt",
    "PIL": "pillow",
    "psycopg": "psycopg",
    "psycopg2": "psycopg2-binary",
    "pydantic_settings": "pydantic-settings",
    "sklearn": "scikit-learn",
    "typing_extensions": "typing-extensions",
    "yaml": "pyyaml",
}

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "an OpenAI-style secret key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "a Google API key"),
]
SECRET_NAME = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)

MIN_COPIED_LITERAL = 80

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"


# ------------------------------------------------------------------ #
#  Capabilities                                                        #
# ------------------------------------------------------------------ #
#
# Each entry names what carries the capability. A rule gated on an absent
# capability is reported UNAVAILABLE so the gap is visible rather than assumed.

CAPABILITY_SOURCE = {
    "env_file": "PR #53 (jiajunh/can-232-...), open against main",
    "editable_install": "no PR -- only on jiajunh/can-228-create-a-skill-...",
    "sweeps_all_files": "no PR -- only on jiajunh/can-228-create-a-skill-...",
    "stub_two_destinations": "PR #51 (feature/all-the-files), open against main",
}


def probe_capabilities():
    """Ask the importable ventis package what it actually supports."""
    caps = dict.fromkeys(CAPABILITY_SOURCE, False)
    caps["ventis"] = False
    try:
        from ventis import stub_generator
    except Exception:  # noqa: BLE001 - a broken install must not crash the check
        return caps

    caps["ventis"] = True
    caps["editable_install"] = hasattr(stub_generator, "_install_step")
    caps["sweeps_all_files"] = hasattr(stub_generator, "_sweep_project_files")
    caps["stub_two_destinations"] = hasattr(stub_generator, "_stub_destinations")

    import importlib

    for module_name in ("ventis.controller.utils.env_file", "ventis.utils.env_file"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001,S112 - the other path is the live one
            continue
        if hasattr(module, "resolve_env_file"):
            caps["env_file"] = True
            break
    return caps


# ------------------------------------------------------------------ #
#  YAML with line numbers                                             #
# ------------------------------------------------------------------ #


class LineDict(dict):
    """A mapping that remembers where it and each of its keys were written."""

    line = 0
    key_lines: ClassVar[dict] = {}


class LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node):
    data = LineDict()
    yield data
    data.update(loader.construct_mapping(node, deep=False))
    data.line = node.start_mark.line + 1
    data.key_lines = {
        key.value: key.start_mark.line + 1
        for key, _ in node.value
        if isinstance(key, yaml.ScalarNode)
    }


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def line_of(mapping, key=None):
    """The source line of `key` inside `mapping`, or of the mapping itself."""
    if not isinstance(mapping, LineDict):
        return 0
    if key is not None:
        return mapping.key_lines.get(key, mapping.line)
    return mapping.line


def load_yaml(path):
    """Parse `path`, returning (data, error). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.load(handle, Loader=LineLoader), None
    except Exception as exc:  # noqa: BLE001 - any parse failure is a finding
        return None, str(exc)


# ------------------------------------------------------------------ #
#  Findings                                                           #
# ------------------------------------------------------------------ #


class Report:
    def __init__(self, project_dir, capabilities):
        self.project_dir = project_dir
        self.capabilities = capabilities
        self.findings = []
        self.reported_ec2_block = False
        # A peer agent is imported by the name of its generated stub, which the
        # build copies flat into every image. Those are not project modules and
        # need no requirement.
        self.stub_module_names = set()

    def add(self, check, level, path, line, summary, mechanism):
        self.findings.append(
            {
                "check": check,
                "level": level,
                "path": self.rel(path) if path else "",
                "line": line or 0,
                "summary": summary,
                "mechanism": mechanism,
            }
        )

    def error(self, check, path, line, summary, mechanism):
        self.add(check, ERROR, path, line, summary, mechanism)

    def warn(self, check, path, line, summary, mechanism):
        self.add(check, WARN, path, line, summary, mechanism)

    def unavailable(self, check, summary):
        self.add(check, INFO, "", 0, summary, "")

    def rel(self, path):
        try:
            return os.path.relpath(path, self.project_dir)
        except ValueError:
            return path

    def counts(self):
        errors = sum(1 for f in self.findings if f["level"] == ERROR)
        warnings = sum(1 for f in self.findings if f["level"] == WARN)
        return errors, warnings


# ------------------------------------------------------------------ #
#  Python source helpers                                             #
# ------------------------------------------------------------------ #


def parse_python(path):
    """AST for `path`, or (None, error). The port is never imported."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        return None, str(exc)
    try:
        return ast.parse(source, filename=path), None
    except SyntaxError as exc:
        return None, f"{exc.msg} (line {exc.lineno})"


def find_class(tree, name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def class_methods(class_node):
    return {
        node.name: node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def parameter_names(func_node):
    """Every parameter a caller can pass by keyword, minus self."""
    args = func_node.args
    positional = [a.arg for a in args.posonlyargs + args.args]
    if positional and positional[0] in ("self", "cls"):
        positional = positional[1:]
    return positional + [a.arg for a in args.kwonlyargs]


def required_parameters(func_node):
    """Parameters with no default, minus self."""
    args = func_node.args
    positional = [a.arg for a in args.posonlyargs + args.args]
    if positional and positional[0] in ("self", "cls"):
        positional = positional[1:]
    if args.defaults:
        positional = positional[: len(positional) - len(args.defaults)]
    kwonly = [
        arg.arg
        for arg, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    ]
    return positional + kwonly


def toplevel_import_names(tree):
    """Top-level package name of every import in the module, with line numbers."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.setdefault(alias.name.split(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.setdefault(node.module.split(".")[0], node.lineno)
    return names


# ------------------------------------------------------------------ #
#  V001-V002  the files parse at all                                  #
# ------------------------------------------------------------------ #


def check_config_loads(report, config_path):
    """V001 -- cli.py _load_config, then cmd_build's config.get("agents", [])."""
    if not os.path.isfile(config_path):
        report.error(
            "V001",
            config_path,
            0,
            "config/global_controller.yaml is missing",
            "cli.py cmd_build logs 'Config file not found' and exits 1.",
        )
        return None
    config, error = load_yaml(config_path)
    if error is not None:
        report.error("V001", config_path, 0, f"unparseable YAML: {error}", "")
        return None
    if not isinstance(config, dict):
        report.error(
            "V001",
            config_path,
            0,
            "the config is empty or is not a mapping",
            "cmd_build calls config.get('agents', []) on it -- AttributeError.",
        )
        return None
    if "agents" not in config:
        report.error(
            "V001",
            config_path,
            line_of(config),
            "no `agents:` key",
            "Nothing is built and nothing is deployed.",
        )
        return config
    agents = config.get("agents")
    if agents is None or not isinstance(agents, list):
        report.error(
            "V001",
            config_path,
            line_of(config, "agents"),
            "`agents:` is null or is not a list",
            "cmd_build iterates it as a list -- TypeError before any image.",
        )
        config["agents"] = []
    return config


def check_agent_yaml_loads(report, path):
    """V002 -- stub_generator reads agent/name with [], not .get()."""
    data, error = load_yaml(path)
    if error is not None:
        report.error("V002", path, 0, f"unparseable YAML: {error}", "")
        return None
    if not isinstance(data, dict):
        report.error(
            "V002",
            path,
            0,
            "the file is empty or is not a mapping",
            "cmd_build does yaml.safe_load(f).get('agent', {}) -- AttributeError.",
        )
        return None
    agent = data.get("agent")
    if not isinstance(agent, dict):
        report.error(
            "V002",
            path,
            line_of(data, "agent"),
            "`agent:` is missing or null",
            "stub_generator does config['agent'] -- KeyError, or AttributeError "
            "in cmd_build's name index.",
        )
        return None
    name = agent.get("name")
    if not isinstance(name, str) or not name:
        report.error(
            "V002",
            path,
            line_of(agent, "name") or line_of(agent),
            "`agent.name` is missing or is not a string",
            "It becomes the generated class name and ENV VENTIS_AGENT_NAME.",
        )
        return None
    if "functions" in agent and agent.get("functions") is None:
        report.error(
            "V002",
            path,
            line_of(agent, "functions"),
            "`functions:` is present but null",
            "stub_generator iterates it -- TypeError: 'NoneType' is not iterable. "
            "Omit the key instead.",
        )
    for func in agent.get("functions") or []:
        if not isinstance(func, dict) or not isinstance(func.get("name"), str):
            report.error(
                "V002",
                path,
                line_of(agent, "functions"),
                "a function entry has no string `name`",
                "stub_generator does func_config['name'] -- KeyError.",
            )
            continue
        if "arguments" in func and func.get("arguments") is None:
            report.error(
                "V002",
                path,
                line_of(func, "arguments"),
                f"`{func['name']}.arguments:` is present but null",
                "stub_generator iterates it -- TypeError. Omit the key instead.",
            )
    return data


# ------------------------------------------------------------------ #
#  V003-V005, V012-V015, V022  the config entries                     #
# ------------------------------------------------------------------ #


def check_config_entries(report, config, config_path, project_dir, yaml_by_name):
    """V003 V004 V005 V012 V013 V014 V015 V022."""
    agents = config.get("agents") or []
    seen_lower = {}
    workflow_entries = []

    for entry in agents:
        if not isinstance(entry, dict):
            report.error(
                "V001",
                config_path,
                line_of(config, "agents"),
                f"an `agents:` item is not a mapping: {entry!r}",
                "cmd_build does agent_cfg['name'] on it.",
            )
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            report.error(
                "V003",
                config_path,
                line_of(entry),
                "an `agents:` entry has no string `name`",
                "cmd_build does agent_cfg['name'] -- KeyError.",
            )
            continue

        # V004 -- the image tag is ventis-<name.lower()>.
        previous = seen_lower.get(name.lower())
        if previous is not None:
            report.error(
                "V004",
                config_path,
                line_of(entry, "name"),
                f"`{name}` and `{previous}` differ only in case",
                "Both build to the image tag ventis-"
                f"{name.lower()}; the second overwrites the first.",
            )
        seen_lower[name.lower()] = name

        entry_type = entry.get("type", "agent")
        if entry_type == "workflow":
            workflow_entries.append((name, entry))
            check_workflow_entry(report, entry, config_path, project_dir)
        else:
            check_agent_entry(
                report, name, entry, config_path, project_dir, yaml_by_name
            )

        check_provider(report, name, entry, config_path)
        check_replicas(report, name, entry, config_path)
        check_requirements(report, name, entry, config_path)
        check_ec2_entry(report, name, entry, config, config_path)

    # V015 -- without a workflow entry nothing serves HTTP.
    if not workflow_entries:
        report.error(
            "V015",
            config_path,
            line_of(config, "agents"),
            "no entry has `type: workflow`",
            "Nothing builds a Flask container, so the port has no HTTP surface.",
        )
    elif len(workflow_entries) > 1:
        names = ", ".join(name for name, _ in workflow_entries)
        report.error(
            "V015",
            config_path,
            line_of(config, "agents"),
            f"more than one `type: workflow` entry: {names}",
            "Every workflow builds into docker_container/Workflow; the last wins.",
        )
    return workflow_entries


def check_agent_entry(report, name, entry, config_path, project_dir, yaml_by_name):
    """V003 V005."""
    entrypoint = entry.get("entrypoint")
    if not entrypoint:
        report.error(
            "V005",
            config_path,
            line_of(entry),
            f"agent `{name}` has no `entrypoint`",
            "cmd_build warns 'Skipping agent', builds no image, and exits 0.",
        )
    elif not os.path.isfile(os.path.join(project_dir, entrypoint)):
        report.error(
            "V005",
            config_path,
            line_of(entry, "entrypoint"),
            f"agent `{name}`: entrypoint `{entrypoint}` does not exist",
            "cmd_build logs 'Agent file not found', skips it, and exits 0.",
        )
    if name not in yaml_by_name:
        report.error(
            "V003",
            config_path,
            line_of(entry, "name"),
            f"no agents/*.yaml declares `agent.name: {name}`",
            "cmd_build warns 'No YAML definition found', builds no image for it, "
            "and exits 0 -- the agent is simply absent from the deployment.",
        )


def check_workflow_entry(report, entry, config_path, project_dir):
    """V015."""
    workflow_file = entry.get("workflow_file")
    if not workflow_file:
        report.error(
            "V015",
            config_path,
            line_of(entry),
            "the workflow entry has no `workflow_file`",
            "cmd_build warns 'Skipping workflow' and exits 0.",
        )
    elif not os.path.isfile(os.path.join(project_dir, workflow_file)):
        report.error(
            "V015",
            config_path,
            line_of(entry, "workflow_file"),
            f"`workflow_file: {workflow_file}` does not exist",
            "cmd_build logs 'Workflow file not found', skips it, and exits 0.",
        )


def check_provider(report, name, entry, config_path):
    """V012 -- provider == "local" is compared case-sensitively; EC2 is not."""
    provider = entry.get("provider", "local")
    if not isinstance(provider, str):
        report.error(
            "V012",
            config_path,
            line_of(entry, "provider"),
            f"`{name}`: provider must be a string, got {provider!r}",
            "InstanceManager compares it to the literal 'local'.",
        )
        return
    if provider == "local" or provider.upper() == "EC2":
        return
    if provider.lower() == "local":
        report.error(
            "V012",
            config_path,
            line_of(entry, "provider"),
            f"`{name}`: `provider: {provider}` must be lowercase `local`",
            "InstanceManager.ensure_instances tests provider == 'local' to "
            "reserve a host port. Any other casing leaves reserved_port None and "
            "Local/_runtime.py dies on int(None) before a container starts.",
        )
    else:
        report.error(
            "V012",
            config_path,
            line_of(entry, "provider"),
            f"`{name}`: unknown `provider: {provider}`",
            "Only 'local' (exact) and 'EC2' (any casing) are recognised.",
        )


def check_replicas(report, name, entry, config_path):
    """V013 -- InstanceManager does int(replicas)."""
    if "replicas" not in entry:
        return
    replicas = entry.get("replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int):
        report.error(
            "V013",
            config_path,
            line_of(entry, "replicas"),
            f"`{name}`: `replicas` must be an int, got {replicas!r}",
            "InstanceManager.ensure_instances does range(int(replicas)); the "
            "list form GlobalController._get_replica_placements accepts raises "
            "TypeError here.",
        )
    elif replicas < 1:
        report.error(
            "V013",
            config_path,
            line_of(entry, "replicas"),
            f"`{name}`: `replicas: {replicas}` launches nothing",
            "range(0) -- the agent is deployed with no instances.",
        )


def check_requirements(report, name, entry, config_path):
    """V014 -- one bad item drops the whole list, with only a warning."""
    if "requirements" not in entry:
        return
    requirements = entry.get("requirements")
    if requirements is None:
        return
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) for item in requirements
    ):
        report.error(
            "V014",
            config_path,
            line_of(entry, "requirements"),
            f"`{name}`: `requirements` must be a list of strings",
            "_normalize_requirements logs one warning and returns [] -- the "
            "whole list is dropped, not the bad item, and the build still "
            "succeeds with none of them installed.",
        )


def check_ec2_entry(report, name, entry, config, config_path):
    """V022 -- the CLI preflight list is shorter than what provisioning needs."""
    provider = entry.get("provider", "local")
    if not isinstance(provider, str) or provider.upper() != "EC2":
        return
    if not entry.get("instance_type"):
        report.error(
            "V022",
            config_path,
            line_of(entry),
            f"`{name}`: EC2 entry has no `instance_type`",
            "EC2/_runtime.py does spec['instance_type'] -- KeyError at provision.",
        )
    if report.reported_ec2_block:
        return
    report.reported_ec2_block = True
    ec2 = config.get("ec2") or {}
    missing = [key for key in EC2_REQUIRED_CONFIG_KEYS if not ec2.get(key)]
    if missing:
        report.error(
            "V022",
            config_path,
            line_of(config, "ec2") or line_of(config),
            f"top-level `ec2:` is missing {', '.join(missing)}",
            "cli.py's preflight checks only four of these; ssh_user is demanded "
            "later by EC2/_runtime.py, after preflight has already passed.",
        )


# ------------------------------------------------------------------ #
#  V006-V010, W005  the adapter against its yaml                      #
# ------------------------------------------------------------------ #

BUILTIN_TYPE_NAMES = frozenset(
    name
    for name in dir(builtins)
    if isinstance(getattr(builtins, name), type) and not name[0].isupper()
)


def check_adapter(report, agent_yaml_path, agent_block, entry, project_dir):
    """V006 V007 V008 V009 V010 W005."""
    name = agent_block["name"]
    functions = agent_block.get("functions") or []
    check_argument_types(report, agent_yaml_path, functions)

    entrypoint = entry.get("entrypoint")
    if not entrypoint:
        return
    entrypoint_path = os.path.join(project_dir, entrypoint)
    if not os.path.isfile(entrypoint_path):
        return

    tree, error = parse_python(entrypoint_path)
    if tree is None:
        report.error(
            "V006",
            entrypoint_path,
            0,
            f"the entrypoint does not parse: {error}",
            "_load_agent exec_module's it and swallows the exception; the first "
            "request answers 'No agent loaded'.",
        )
        return

    class_node = find_class(tree, name)
    if class_node is None:
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        found = ", ".join(classes) if classes else "no classes at all"
        report.error(
            "V006",
            entrypoint_path,
            1,
            f"no class named `{name}` at module level (found: {found})",
            "_load_agent does getattr(module, VENTIS_AGENT_NAME) and swallows "
            "the AttributeError. The class name must equal agent.name exactly.",
        )
        return

    methods = class_methods(class_node)
    check_constructor(report, entrypoint_path, name, methods)

    for func in functions:
        if not isinstance(func, dict) or not isinstance(func.get("name"), str):
            continue
        check_method(report, entrypoint_path, agent_yaml_path, name, func, methods)


def check_argument_types(report, agent_yaml_path, functions):
    """V010 -- the type string is pasted into an ast.Name, never checked."""
    for func in functions:
        if not isinstance(func, dict):
            continue
        for arg in func.get("arguments") or []:
            if not isinstance(arg, dict) or "type" not in arg:
                continue
            declared = arg.get("type")
            if not isinstance(declared, str):
                report.error(
                    "V010",
                    agent_yaml_path,
                    line_of(arg, "type"),
                    f"`type: {declared!r}` is not a string",
                    "ast.Name(id=<type>) then ast.unparse -- a non-string raises "
                    "while the stub is generated.",
                )
                continue
            if declared in BUILTIN_TYPE_NAMES:
                continue
            report.error(
                "V010",
                agent_yaml_path,
                line_of(arg, "type"),
                f"`type: {declared}` is not a builtin",
                "stub_generator pastes it verbatim into the generated "
                "annotation, and the stub module imports only Future and "
                "inspect. Anything else raises NameError when the stub is "
                "imported -- after a green build. Use str int float bool dict "
                "list.",
            )


def check_constructor(report, entrypoint_path, name, methods):
    """V007 -- _load_agent calls agent_class() with no arguments."""
    init = methods.get("__init__")
    if init is None:
        return
    required = required_parameters(init)
    if required:
        report.error(
            "V007",
            entrypoint_path,
            init.lineno,
            f"`{name}.__init__` requires {', '.join(required)}",
            "_load_agent calls agent_class() with no arguments; the TypeError is "
            "swallowed and the first request answers 'No agent loaded'. Read "
            "configuration from the environment inside __init__ instead.",
        )


def check_method(report, entrypoint_path, agent_yaml_path, class_name, func, methods):
    """V008 V009 W005."""
    func_name = func["name"]
    method = methods.get(func_name)
    if method is None:
        report.error(
            "V008",
            entrypoint_path,
            0,
            f"`{class_name}` has no method `{func_name}`",
            "The yaml declares it, so callers get a stub for it; the controller "
            f"then answers \"Agent {class_name} has no method '{func_name}'\".",
        )
        return

    # V009 -- nothing on the execution path awaits.
    if isinstance(method, ast.AsyncFunctionDef):
        report.error(
            "V009",
            entrypoint_path,
            method.lineno,
            f"`{class_name}.{func_name}` is `async def`",
            "The executor calls method(**args) with no await, so Redis receives "
            "'<coroutine object ...>'. Keep the signature synchronous and call "
            "asyncio.run(...) inside the body.",
        )

    # V008 -- the controller calls method(**args) with the yaml's names.
    declared = [
        arg["name"]
        for arg in func.get("arguments") or []
        if isinstance(arg, dict) and isinstance(arg.get("name"), str)
    ]
    actual = parameter_names(method)
    required = required_parameters(method)

    missing = [arg for arg in declared if arg not in actual]
    if missing:
        report.error(
            "V008",
            entrypoint_path,
            method.lineno,
            f"`{class_name}.{func_name}` has no parameter "
            f"{', '.join(repr(m) for m in missing)}, but the yaml declares it",
            "LocalController does method(**args) with the yaml's argument names. "
            "A mismatch is TypeError: unexpected keyword argument, at request "
            f"time. See {os.path.basename(agent_yaml_path)}.",
        )

    unfilled = [arg for arg in required if arg not in declared]
    if unfilled:
        report.error(
            "V008",
            entrypoint_path,
            method.lineno,
            f"`{class_name}.{func_name}` requires {', '.join(unfilled)}, which "
            "the yaml does not declare",
            "Only declared arguments are ever sent, and the generated stub gives "
            "none of them a default. Declare them in the yaml or default them "
            "in the signature.",
        )

    # W005 -- `returns` is read by nothing, but it is how the workflow author
    # learns the call site needs json.loads.
    returns = func.get("returns")
    declared_return = returns.get("type") if isinstance(returns, dict) else None
    annotation = method.returns
    annotated = annotation.id if isinstance(annotation, ast.Name) else None
    if annotated in ("dict", "list") and declared_return != annotated:
        report.warn(
            "W005",
            entrypoint_path,
            method.lineno,
            f"`{class_name}.{func_name}` returns {annotated} but the yaml "
            f"declares `returns.type: {declared_return}`",
            "`returns` is read by nothing; its only job is telling whoever "
            "writes the workflow that .value() hands back a string to json.loads.",
        )


# ------------------------------------------------------------------ #
#  V016-V018  the workflow                                            #
# ------------------------------------------------------------------ #


def check_workflow(report, workflow_path):
    """V016 V017 V018."""
    tree, error = parse_python(workflow_path)
    if tree is None:
        report.error("V016", workflow_path, 0, f"does not parse: {error}", "")
        return

    main = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "main"
        ):
            main = node
            break

    if main is None:
        defined = [
            n.name
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        found = ", ".join(defined) if defined else "no top-level functions"
        report.error(
            "V016",
            workflow_path,
            1,
            f"no top-level function named `main` (found: {found})",
            "Ventis serves POST /<fn.__name__>, but the deployment platform's "
            "test endpoint posts to a hardcoded /main. A differently named "
            "workflow builds, deploys and stays unreachable -- 404, container "
            "healthy.",
        )
    else:
        check_main_signature(report, workflow_path, main)

    # V016 -- deploy() is what starts Flask.
    if not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "deploy"
        for node in ast.walk(tree)
    ):
        report.error(
            "V016",
            workflow_path,
            1,
            "the workflow never calls `deploy(...)`",
            "workflow_launcher.py exec's this file and nothing else starts the "
            "HTTP server; the container comes up serving nothing.",
        )

    check_main_guard(report, workflow_path, tree)
    check_fused_fanout(report, workflow_path, tree)


def check_main_signature(report, workflow_path, main):
    """V016 -- the platform sends exactly {"query": ...}."""
    if isinstance(main, ast.AsyncFunctionDef):
        report.error(
            "V016",
            workflow_path,
            main.lineno,
            "`main` is `async def`",
            "deploy() calls workflow_fn(**kwargs) on a Flask worker thread with "
            "no await; the response body would be a coroutine repr.",
        )
    params = parameter_names(main)
    if not params:
        report.error(
            "V016",
            workflow_path,
            main.lineno,
            "`main` takes no arguments",
            'The platform posts {"query": "..."} and deploy() splats the '
            "body in as kwargs -- TypeError on every request.",
        )
        return
    if params[0] != "query":
        report.error(
            "V016",
            workflow_path,
            main.lineno,
            f"`main`'s first parameter is `{params[0]}`, not `query`",
            "The platform's body schema is strictly validated as "
            "{query: string}; any other key is rejected with 400 in the control "
            "plane, before the request reaches the host.",
        )
    extra = [p for p in required_parameters(main) if p != "query"]
    if extra:
        report.error(
            "V016",
            workflow_path,
            main.lineno,
            f"`main` requires {', '.join(extra)} beyond `query`",
            "Only `query` is ever sent, so every other parameter needs a "
            "default or the call raises on every request. Pack richer input "
            "into `query`.",
        )


def check_main_guard(report, workflow_path, tree):
    """V017 -- the workflow is exec'd, so __name__ == "__main__"."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__"
                for c in test.comparators
            )
        ):
            report.error(
                "V017",
                workflow_path,
                node.lineno,
                '`if __name__ == "__main__":` block in the workflow',
                "workflow_launcher.py runs exec(open(<workflow>).read()), so "
                "__name__ IS '__main__' here and this block executes in "
                "production, at container start.",
            )


def check_fused_fanout(report, workflow_path, tree):
    """V018 -- .value() blocks, so dispatching and resolving in one
    comprehension runs the fan-out one call at a time."""
    comprehensions = (ast.ListComp, ast.SetComp, ast.GeneratorExp)
    for node in ast.walk(tree):
        if not isinstance(node, comprehensions + (ast.DictComp,)):
            continue
        elements = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        for element in elements:
            for inner in ast.walk(element):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "value"
                    and isinstance(inner.func.value, ast.Call)
                ):
                    report.error(
                        "V018",
                        workflow_path,
                        node.lineno,
                        "one comprehension both dispatches a call and resolves "
                        "it with .value()",
                        ".value() blocks, so each call completes before the "
                        "next is dispatched. It does not error -- the fan-out "
                        "is just silently serial, and with it the reason to be "
                        "on Ventis. Dispatch every call first, then resolve: "
                        "futures = [a.work(i) for i in items] then "
                        "[f.value() for f in futures].",
                    )
                    return


# ------------------------------------------------------------------ #
#  V019-V020  what the copy order overwrites                          #
# ------------------------------------------------------------------ #


def check_flat_collisions(report, project_dir, yaml_paths, entrypoints):
    """V019 V020 -- later copies land on earlier ones at the context root."""
    for entry in sorted(os.listdir(project_dir)):
        path = os.path.join(project_dir, entry)
        if not os.path.isfile(path) or not entry.endswith(".py"):
            continue

        # V019 -- the shared runtime is copied flat, after the project sweep.
        if entry in RUNTIME_FLAT_NAMES:
            report.error(
                "V019",
                path,
                1,
                f"a project module named `{entry}` sits at the project root",
                "The shared Ventis runtime is copied flat into the image after "
                "the project sweep, so this file is overwritten by Ventis's own "
                f"{entry}. Rename it or move it into a package directory.",
            )

    # V020 -- a stub is copied flat under its yaml's basename.
    entrypoint_basenames = {os.path.basename(e) for e in entrypoints if e}
    for yaml_path in yaml_paths:
        stem = os.path.splitext(os.path.basename(yaml_path))[0]
        module = f"{stem}.py"
        if module in entrypoint_basenames:
            continue  # the entrypoint is copied last and wins its flat name back
        candidate = os.path.join(project_dir, module)
        if os.path.isfile(candidate):
            report.error(
                "V020",
                candidate,
                1,
                f"`{os.path.basename(yaml_path)}` generates a stub that lands on "
                f"`{module}`",
                "The yaml's basename names the stub, and the stub is copied flat "
                "over the swept tree. Anything importing this module inside the "
                "container gets the generated stub instead of the real code. "
                "Rename the yaml to match its own entrypoint.",
            )


# ------------------------------------------------------------------ #
#  V021  policy.yaml                                                  #
# ------------------------------------------------------------------ #


def check_policy(report, config, config_path):
    """V021 -- absent is fine; present-but-empty kills deploy."""
    policy_path = os.path.join(
        os.path.dirname(os.path.abspath(config_path)), "policy.yaml"
    )
    if not os.path.isfile(policy_path):
        # _load_policy_rules logs and returns [], and _check_policy allows
        # everything when the rule list is empty. Nothing to check.
        return

    policy, error = load_yaml(policy_path)
    if error is not None:
        report.error("V021", policy_path, 0, f"unparseable YAML: {error}", "")
        return
    if not isinstance(policy, dict):
        report.error(
            "V021",
            policy_path,
            0,
            "policy.yaml exists but is empty",
            "_load_policy_rules does policy_config.get('rules', []) on None -- "
            "AttributeError inside GlobalController.__init__, so `ventis deploy` "
            "dies before any container starts. Delete the file instead; absent "
            "means everything is allowed.",
        )
        return

    rules = policy.get("rules")
    if rules is None or not isinstance(rules, list):
        report.error(
            "V021",
            policy_path,
            line_of(policy, "rules") or line_of(policy),
            "`rules:` is null or is not a list",
            "_load_policy_rules calls .sort() on it -- AttributeError inside "
            "GlobalController.__init__, before any container starts.",
        )
        return

    declared = [
        entry.get("name")
        for entry in config.get("agents") or []
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    fallback = None
    for rule in rules:
        if not isinstance(rule, dict):
            report.error(
                "V021",
                policy_path,
                line_of(policy, "rules"),
                f"a rule is not a mapping: {rule!r}",
                "_check_policy does rule.get('match', {}) on it.",
            )
            continue
        match = rule.get("match")
        if match is None or (isinstance(match, dict) and not match):
            fallback = rule

    if fallback is None:
        report.error(
            "V021",
            policy_path,
            line_of(policy, "rules"),
            "no rule with an empty `match: {}`",
            "_check_policy denies access when no rule matches the request "
            "context, so every service answers Unauthorized after its request "
            "was already accepted with a 202.",
        )
        return

    if not isinstance(fallback.get("access"), (list, str)):
        report.error(
            "V021",
            policy_path,
            line_of(fallback, "access") or line_of(fallback),
            "the `match: {}` rule's `access` is neither a list nor `all`",
            "_check_policy does `service in access`.",
        )
        return

    # Reachable under *some* context. A service deliberately restricted to one
    # caller -- text2sql keeps ProductionExecutorAgent out of the fallback and
    # reaches it only through an `access: all` rule -- is correct policy, not a
    # defect, so only a service no rule can ever reach is worth reporting.
    reachable = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        access = rule.get("access")
        if access == "all":
            reachable.update(declared)
        elif isinstance(access, list):
            reachable.update(item for item in access if isinstance(item, str))

    unreachable = [name for name in declared if name not in reachable]
    if unreachable:
        report.warn(
            "V021",
            policy_path,
            line_of(policy, "rules"),
            f"no rule grants access to {', '.join(unreachable)}",
            "The first matching rule decides, and a service named in none of "
            "them answers Unauthorized on /status after the request was "
            "already accepted with a 202. Intentional if the service is meant "
            "to be unreachable.",
        )


# ------------------------------------------------------------------ #
#  V030-V031  capability-gated rules                                  #
# ------------------------------------------------------------------ #


def check_env_file(report, config, config_path, project_dir):
    """V030 -- gated on the env_file support that PR #53 carries."""
    declared = config.get("env_file")
    supported = report.capabilities.get("env_file")

    if not supported:
        if declared:
            report.error(
                "V030",
                config_path,
                line_of(config, "env_file"),
                f"`env_file: {declared}` is set, but this Ventis never reads it",
                "No resolve_env_file in the importable ventis package, so the "
                "key is silently dropped and the container answers a provider "
                "credential error on the first request. It arrives with "
                f"{CAPABILITY_SOURCE['env_file']}.",
            )
        else:
            report.unavailable(
                "V030",
                "env_file is not supported by the importable Ventis "
                f"({CAPABILITY_SOURCE['env_file']}). Credentials have no "
                "declared path into a container on this tree.",
            )
        return

    if not declared:
        report.warn(
            "V030",
            config_path,
            line_of(config),
            "no `env_file:` in the config",
            "Only five VENTIS_* variables reach a container without it. If the "
            "source reads any credential from the environment, the first "
            "request fails on a provider error.",
        )
        return

    resolved = os.path.expanduser(str(declared))
    if not os.path.isabs(resolved):
        resolved = os.path.join(project_dir, resolved)
    if not os.path.isfile(resolved):
        report.error(
            "V030",
            config_path,
            line_of(config, "env_file"),
            f"`env_file: {declared}` does not resolve to a file",
            "resolve_env_file raises before GlobalController exists, so "
            "`ventis deploy` fails with one error line. The path is resolved "
            "against the project root you run from.",
        )
    elif not os.access(resolved, os.R_OK):
        report.error(
            "V030",
            config_path,
            line_of(config, "env_file"),
            f"`env_file: {declared}` is not readable",
            "resolve_env_file raises on an unreadable file.",
        )


def check_import_root(report, project_dir, entrypoint_paths):
    """V031 -- gated on the editable install, which no PR carries today."""
    supported = report.capabilities.get("editable_install")
    has_metadata = any(
        os.path.isfile(os.path.join(project_dir, name))
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )

    non_flat = []
    for path in entrypoint_paths:
        tree, _ = parse_python(path)
        if tree is None:
            continue
        for name, lineno in toplevel_import_names(tree).items():
            if name in report.stub_module_names or f"{name}.py" in RUNTIME_FLAT_NAMES:
                continue
            if _resolves_flat(project_dir, name):
                continue
            location = _resolves_nested(project_dir, name)
            if location:
                non_flat.append((path, lineno, name, location))

    if not supported:
        report.unavailable(
            "V031",
            "the editable install (`-e .`) is not supported by the importable "
            f"Ventis ({CAPABILITY_SOURCE['editable_install']}). Only modules "
            "that land flat at /app import inside a container.",
        )
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, which is not at the "
                "project root",
                "sys.path[0] is /app and this Ventis runs no editable install, "
                "so only modules swept to the root import. The adapter raises "
                "ModuleNotFoundError inside _load_agent and the first request "
                "answers 'No agent loaded'.",
            )
        return

    if non_flat and not has_metadata:
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, and the project root "
                "has no packaging metadata",
                "A pyproject.toml, setup.py or setup.cfg at the root is what "
                "adds `-e .`, and the project's own metadata is what decides the "
                "import root. Without it the install is skipped silently and "
                "only flat modules import.",
            )


def _resolves_flat(project_dir, name):
    return os.path.isfile(os.path.join(project_dir, f"{name}.py")) or os.path.isfile(
        os.path.join(project_dir, name, "__init__.py")
    )


def _resolves_nested(project_dir, name):
    """Where inside the tree `name` lives, if it is a project module at all."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        if root == project_dir:
            continue
        if f"{name}.py" in files:
            return os.path.relpath(os.path.join(root, f"{name}.py"), project_dir)
        if name in dirs and os.path.isfile(os.path.join(root, name, "__init__.py")):
            return os.path.relpath(os.path.join(root, name), project_dir)
    return None


# ------------------------------------------------------------------ #
#  W001-W006  the rewrite smells                                      #
# ------------------------------------------------------------------ #
#
# Warnings, not errors: each is a heuristic, and a false positive must never
# block a correct port. --strict promotes them for CI.


def _string_literals(tree, minimum):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and len(node.value.strip()) >= minimum
        ):
            yield node.value, node.lineno


def check_copied_literals(report, project_dir, port_paths, source_paths):
    """W001 -- a prompt that exists in the source and again in the adapter."""
    source_text = {}
    for path in source_paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source_text[path] = handle.read()
        except OSError:
            continue
    if not source_text:
        return

    for port_path in port_paths:
        tree, _ = parse_python(port_path)
        if tree is None:
            continue
        for literal, lineno in _string_literals(tree, MIN_COPIED_LITERAL):
            for source_path, text in source_text.items():
                if literal in text:
                    report.warn(
                        "W001",
                        port_path,
                        lineno,
                        f"a {len(literal)}-character string literal also appears "
                        f"in {report.rel(source_path)}",
                        "It exists in the source. Import it -- the whole tree is "
                        "in the image, and a copy drifts the moment the source "
                        "changes. A port that restates a prompt has rewritten "
                        "the project, not ported it.",
                    )
                    break


def check_secrets(report, port_paths):
    """W003 -- env_file is the way in; nothing else is."""
    for path in port_paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, start=1):
            for pattern, description in SECRET_PATTERNS:
                if pattern.search(line):
                    report.warn(
                        "W003",
                        path,
                        number,
                        f"this line looks like {description}",
                        "Never put a secret in the source tree or the build "
                        "context. The build sweeps the project into every "
                        "image.",
                    )
                    break

        tree, _ = parse_python(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not (
                isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.value.value.strip()
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and SECRET_NAME.search(target.id):
                    report.warn(
                        "W003",
                        path,
                        node.lineno,
                        f"`{target.id}` is assigned a literal string",
                        "Read it from the environment instead; the build sweeps "
                        "this file into every image.",
                    )


def check_source_tree_clean(report, project_dir):
    """W002 -- the port must leave `git status` on the source clean."""

    def git(*args):
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    # `git status` prints paths relative to the repo root, so a port nested
    # inside a larger repo needs that prefix stripped before the port's own
    # directories can be recognised.
    prefix = git("rev-parse", "--show-prefix")
    if prefix is None:
        return
    prefix = prefix.strip()
    status = git("status", "--porcelain", "--", ".")
    if status is None:
        return

    port_prefixes = ("agents/", "workflow/", "config/")
    generated = ("docker_container/", "stubs/", "grpc_stubs/")
    for line in status.splitlines():
        path = line[3:].strip().strip('"')
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        if not path or path.startswith(port_prefixes) or path.startswith(generated):
            continue
        report.warn(
            "W002",
            os.path.join(project_dir, path),
            0,
            f"`{path}` is modified or untracked outside the port's own files",
            "A port adds agents/, workflow/ and config/ beside an untouched "
            "source tree. If this is an edit to the source, it is a rewrite; if "
            "it is unrelated local work, ignore this line.",
        )


def check_requirements_coverage(
    report, project_dir, entry, entrypoint_path, config_path
):
    """W006 -- an import the container cannot satisfy."""
    tree, _ = parse_python(entrypoint_path)
    if tree is None:
        return

    declared = {
        _normalize_distribution(item)
        for item in (entry.get("requirements") or [])
        if isinstance(item, str)
    }
    base = {_normalize_distribution(item) for item in BASE_AGENT_REQUIREMENTS}
    stdlib = getattr(sys, "stdlib_module_names", frozenset())

    for name, lineno in sorted(toplevel_import_names(tree).items()):
        if name in stdlib or name == "ventis":
            continue
        # Provided by the image itself: the shared runtime is copied flat, and
        # every agents/*.yaml generates a stub that is copied flat too.
        if f"{name}.py" in RUNTIME_FLAT_NAMES or name in report.stub_module_names:
            continue
        if _resolves_flat(project_dir, name) or _resolves_nested(project_dir, name):
            continue
        distribution = _normalize_distribution(IMPORT_TO_DISTRIBUTION.get(name, name))
        if distribution in base or distribution in declared:
            continue
        report.warn(
            "W006",
            entrypoint_path,
            lineno,
            f"`import {name}` is in neither the runtime's base list nor this "
            "entry's `requirements:`",
            "The container installs the base list plus `requirements:` and "
            "nothing else, so this is a ModuleNotFoundError inside _load_agent "
            "and 'No agent loaded' on the first request. If the distribution is "
            f"named something other than `{name}`, declare that name in "
            f"{report.rel(config_path)}.",
        )


def _normalize_distribution(name):
    return re.split(r"[<>=!\[;\s]", name.strip().lower(), maxsplit=1)[0].replace(
        "_", "-"
    )


# ------------------------------------------------------------------ #
#  Driver                                                             #
# ------------------------------------------------------------------ #


def validate(project_dir, config_path, capabilities):
    report = Report(project_dir, capabilities)

    config = check_config_loads(report, config_path)
    if config is None:
        return report

    import glob

    yaml_paths = sorted(glob.glob(os.path.join(project_dir, "agents", "*.yaml")))
    if not yaml_paths:
        report.error(
            "V002",
            os.path.join(project_dir, "agents"),
            0,
            "no agents/*.yaml files",
            "cmd_build warns 'No agent YAML files found'; no stubs are generated "
            "and no agent image is built.",
        )

    report.stub_module_names = {
        os.path.splitext(os.path.basename(path))[0] for path in yaml_paths
    }

    agents_by_name = {}
    for path in yaml_paths:
        data = check_agent_yaml_loads(report, path)
        if data is not None:
            agents_by_name[data["agent"]["name"]] = (path, data["agent"])

    entries = config.get("agents") or []
    check_config_entries(report, config, config_path, project_dir, set(agents_by_name))

    entrypoints = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") == "workflow":
            continue
        name = entry.get("name")
        entrypoint = entry.get("entrypoint")
        if entrypoint:
            entrypoints.append(entrypoint)
        if name in agents_by_name:
            yaml_path, agent_block = agents_by_name[name]
            check_adapter(report, yaml_path, agent_block, entry, project_dir)
        entrypoint_path = os.path.join(project_dir, entrypoint or "")
        if entrypoint and os.path.isfile(entrypoint_path):
            check_requirements_coverage(
                report, project_dir, entry, entrypoint_path, config_path
            )

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") != "workflow":
            continue
        workflow_file = entry.get("workflow_file")
        if not workflow_file:
            continue
        workflow_path = os.path.join(project_dir, workflow_file)
        if os.path.isfile(workflow_path):
            check_workflow(report, workflow_path)

    check_flat_collisions(report, project_dir, yaml_paths, entrypoints)
    check_policy(report, config, config_path)
    check_env_file(report, config, config_path, project_dir)

    entrypoint_paths = [
        os.path.join(project_dir, e)
        for e in entrypoints
        if os.path.isfile(os.path.join(project_dir, e))
    ]
    check_import_root(report, project_dir, entrypoint_paths)

    port_paths = list(entrypoint_paths)
    for entry in entries:
        if isinstance(entry, dict) and entry.get("workflow_file"):
            candidate = os.path.join(project_dir, entry["workflow_file"])
            if os.path.isfile(candidate):
                port_paths.append(candidate)

    source_paths = _source_paths(project_dir, port_paths)
    check_copied_literals(report, project_dir, port_paths, source_paths)
    check_secrets(report, port_paths)
    check_source_tree_clean(report, project_dir)
    return report


def _source_paths(project_dir, port_paths):
    """Every project .py that is not one of the port's own four files."""
    excluded = {os.path.abspath(p) for p in port_paths}
    found = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d != "__pycache__"
            and not (
                root == project_dir and d in ("docker_container", "stubs", "grpc_stubs")
            )
        ]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) not in excluded:
                found.append(path)
    return found


# ------------------------------------------------------------------ #
#  Output                                                             #
# ------------------------------------------------------------------ #

LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


def _wrap(text, width, indent):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) + len(indent) > width and current:
            lines.append(indent + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def print_report(report, project_dir):
    caps = report.capabilities
    if not caps.get("ventis"):
        print("ventis is not importable here -- capability-gated rules are")
        print("reported UNAVAILABLE rather than checked.\n")
    else:
        print("Ventis capabilities detected:")
        for key, source in CAPABILITY_SOURCE.items():
            mark = "yes" if caps.get(key) else "no "
            print(f"  {mark}  {key:<22} {source}")
        print()

    findings = sorted(
        report.findings,
        key=lambda f: (LEVEL_ORDER[f["level"]], f["check"], f["path"], f["line"]),
    )
    for finding in findings:
        where = finding["path"]
        if where and finding["line"]:
            where = f"{where}:{finding['line']}"
        header = f"{finding['check']}  {finding['level']:<5}"
        print(f"{header}  {where}" if where else header)
        for line in _wrap(finding["summary"], 78, "    "):
            print(line)
        if finding["mechanism"]:
            for line in _wrap(finding["mechanism"], 78, "      "):
                print(line)
        print()

    errors, warnings = report.counts()
    if not findings:
        print(f"{project_dir}: clean.")
        return
    print(f"{errors} error(s), {warnings} warning(s).")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check a Ventis port against the rules in SKILL.md."
    )
    parser.add_argument(
        "project_dir", nargs="?", default=".", help="the port's project root"
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path relative to project_dir (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--strict", action="store_true", help="fail on warnings as well as errors"
    )
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.project_dir)
    config_path = (
        args.config
        if os.path.isabs(args.config)
        else os.path.join(project_dir, args.config)
    )

    capabilities = probe_capabilities()
    report = validate(project_dir, config_path, capabilities)
    errors, warnings = report.counts()

    if args.json:
        print(
            json.dumps(
                {
                    "project_dir": project_dir,
                    "capabilities": capabilities,
                    "errors": errors,
                    "warnings": warnings,
                    "findings": report.findings,
                },
                indent=2,
            )
        )
    else:
        print_report(report, report.rel(project_dir) or project_dir)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
