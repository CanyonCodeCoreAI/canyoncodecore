#!/usr/bin/env python3
"""Preflight the runtime traps that `canyonos build` cannot see.

This deliberately does not duplicate build-time validation such as malformed
YAML, missing entrypoints, or config-to-yaml matching. `canyonos build` owns those
checks. This script parses Python without importing it and catches failures that
otherwise stay hidden until a container loads an agent, starts a workflow, or
serves its first request. A replica is not evidence: the controller writes
`healthy` to Redis before `_load_agent` runs.

    python validate.py [project_dir] [-c config/global_controller.yaml]
                       [--json] [--strict]

Exit 1 if any ERROR was reported, 0 otherwise. --strict also fails on warnings.

Runtime capabilities vary across CanyonOS Core installations. This script probes
the importable `canyonos` package directly. A capability-gated check reports
UNAVAILABLE when its behavior cannot be proven.
"""

import argparse
import ast
import builtins
import json
import os
import re
import sys
from typing import ClassVar

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a CanyonOS Core dependency
    sys.stderr.write("validate.py needs pyyaml: pip install pyyaml\n")
    raise SystemExit(2) from None


DEFAULT_CONFIG_PATH = "config/global_controller.yaml"

# Copied flat into every image over the swept project tree, so a project module
# landing flat under one of these names is overwritten.
# canyonos/stub_generator.py generate_docker / generate_workflow_docker.
RUNTIME_FLAT_NAMES = frozenset(
    {
        "future.py",
        "canyonos_context.py",
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

# canyonos/stub_generator.py BASE_AGENT_REQUIREMENTS / BASE_WORKFLOW_REQUIREMENTS.
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

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"


# ------------------------------------------------------------------ #
#  Capabilities                                                        #
# ------------------------------------------------------------------ #
#
# Stable labels for behavior detected from the importable runtime. They contain
# no external development metadata.

CAPABILITY_SOURCE = {
    "env_file": "runtime env-file injection",
    "editable_install": "editable project installation",
    "sweeps_all_files": "full project-file sweep",
    "stub_two_destinations": "flat and package stub destinations",
}


def probe_capabilities():
    """Ask the importable canyonos package what it actually supports."""
    caps = dict.fromkeys(CAPABILITY_SOURCE, False)
    caps["canyonos_core"] = False
    try:
        from canyonos_core import stub_generator
    except Exception:  # noqa: BLE001 - a broken install must not crash the check
        return caps

    caps["canyonos_core"] = True
    caps["editable_install"] = hasattr(stub_generator, "_install_step")
    caps["sweeps_all_files"] = hasattr(stub_generator, "_sweep_project_files")
    caps["stub_two_destinations"] = hasattr(stub_generator, "_stub_destinations")

    import importlib

    for module_name in ("canyonos_core.controller.utils.env_file", "canyonos_core.utils.env_file"):
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
#  V006-V010  adapter failures hidden by _load_agent                 #
# ------------------------------------------------------------------ #

BUILTIN_TYPE_NAMES = frozenset(
    name
    for name in dir(builtins)
    if isinstance(getattr(builtins, name), type) and not name[0].isupper()
)


def check_adapter(report, agent_yaml_path, agent_block, entry, project_dir):
    """V006 V007 V008 V009 V010."""
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
            "_load_agent does getattr(module, CANYONOS_AGENT_NAME) and swallows "
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
                continue  # stub generation reports malformed type values
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
    """V008 V009."""
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



# ------------------------------------------------------------------ #
#  V016-V018  the workflow                                            #
# ------------------------------------------------------------------ #


def check_stub_imports(report, workflow_path, tree, stub_classes):
    """V023 -- the workflow must import a stub as `from agents.<basename> import <AgentName>`.

    The build copies each stub to exactly one path, and for the workflow image
    that path is agents/<basename>.py. Two ways of writing this line fail, and
    the project walks you into both: the flat form is what examples/ uses, and
    the class name is the one `canyonos build` prints, which is not the one it
    writes.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                base = alias.name.split(".")[0]
                if base in stub_classes:
                    report.error(
                        "V023", workflow_path, node.lineno,
                        f"`import {alias.name}` -- the stub is at "
                        f"agents/{base}.py, not flat",
                        "The build copies a stub to one path, and for the "
                        "workflow that path is under agents/. This is a "
                        "ModuleNotFoundError the moment the workflow runs. "
                        f"Write `from agents.{base} import {stub_classes[base]}`.",
                    )
            continue

        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue

        module = node.module
        if module in stub_classes:
            report.error(
                "V023", workflow_path, node.lineno,
                f"`from {module} import ...` -- the stub is at "
                f"agents/{module}.py, not flat",
                "The build copies a stub to one path, and for the workflow "
                "that path is under agents/. The flat form is what this "
                "repository's own examples use and it raises "
                "ModuleNotFoundError in the workflow image. Write "
                f"`from agents.{module} import {stub_classes[module]}`.",
            )
            continue

        if not module.startswith("agents."):
            continue
        base = module.split(".", 1)[1]
        expected = stub_classes.get(base)
        if expected is None:
            continue
        for alias in node.names:
            if alias.name == expected:
                continue
            if alias.name == f"{expected}Stub":
                report.error(
                    "V023", workflow_path, node.lineno,
                    f"`{alias.name}` is the name the build prints, not the "
                    f"class it writes",
                    "generate_agent_stub sets class_name = agent_config['name'] "
                    "and then recomputes it with a 'Stub' suffix for the log "
                    "line only. The message names a class that does not exist; "
                    f"the class is `{expected}`.",
                )
            else:
                report.error(
                    "V023", workflow_path, node.lineno,
                    f"`{alias.name}` is not a class the stub for {base} defines",
                    f"The stub's class carries the agent's own name: `{expected}`.",
                )


def check_workflow(report, workflow_path, stub_classes=None):
    """V016 V017 V018 V023."""
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
            "CanyonOS Core serves POST /<fn.__name__>, but the deployment platform's "
            "test endpoint posts to a hardcoded /main. A differently named "
            "workflow builds, deploys and stays unreachable -- 404, container "
            "healthy.",
        )
    else:
        check_main_signature(report, workflow_path, main)

    if stub_classes:
        check_stub_imports(report, workflow_path, tree, stub_classes)

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
                        "on CanyonOS Core. Dispatch every call first, then resolve: "
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
                "The shared CanyonOS Core runtime is copied flat into the image after "
                "the project sweep, so this file is overwritten by CanyonOS Core's own "
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
#  V030-V031  capability-gated rules                                  #
# ------------------------------------------------------------------ #


def check_env_file(report, config, config_path, project_dir):
    """V030 -- gated on detected env-file injection support."""
    declared = config.get("env_file")
    supported = report.capabilities.get("env_file")

    if not supported:
        if declared:
            report.error(
                "V030",
                config_path,
                line_of(config, "env_file"),
                f"`env_file: {declared}` is set, but this CanyonOS Core never reads it",
                "No resolve_env_file in the importable canyonos package, so the "
                "key is silently dropped and the container answers a provider "
                "credential error on the first request. This port requires the "
                "`env_file` runtime capability.",
            )
        else:
            report.unavailable(
                "V030",
                "env_file is not supported by the importable `canyonos` runtime. "
                "Credentials have no declared path into a container on this tree.",
            )
        return

    if not declared:
        report.warn(
            "V030",
            config_path,
            line_of(config),
            "no `env_file:` in the config",
            "Only runtime-managed CANYONOS_* variables are guaranteed without it. "
            "If the source reads credentials from the environment, the first "
            "request fails on a provider error.",
        )
        return

    # Path existence and readability are deploy-preflight checks. Do not
    # duplicate them here.


def check_import_root(report, project_dir, entrypoint_paths):
    """V031 -- gated on detected editable-install support."""
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
            "`canyonos` runtime. Only names rooted at /app import inside a container.",
        )
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, which is not at the "
                "project root",
                "sys.path[0] is /app and this CanyonOS Core runs no editable install, "
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
                "A pyproject.toml, setup.py or setup.cfg at the port root is "
                "what adds `-e .`; metadata nested inside the untouched source "
                "tree is ignored. Add minimal root metadata pointing at the "
                "existing package directory. Without it the install is skipped "
                "silently.",
            )


def _resolves_flat(project_dir, name):
    """Whether Python can resolve `name` with /app as its import root.

    A directory does not need __init__.py: PEP 420 namespace packages resolve
    from sys.path just like regular packages.
    """
    return os.path.isfile(os.path.join(project_dir, f"{name}.py")) or os.path.isdir(
        os.path.join(project_dir, name)
    )


def _resolves_nested(project_dir, name):
    """Where below /app `name` lives but cannot resolve as a top-level name."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        if root == project_dir:
            continue
        if f"{name}.py" in files:
            return os.path.relpath(os.path.join(root, f"{name}.py"), project_dir)
        if name in dirs:
            return os.path.relpath(os.path.join(root, name), project_dir)
    return None


# ------------------------------------------------------------------ #
#  W003, W006  secrets and imports a green build does not reject      #
# ------------------------------------------------------------------ #


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


def _pyproject_dependencies(project_dir):
    """What `-e .` installs alongside `requirements:`, or None if unreadable.

    None and the empty set mean different things here: empty means the project
    declares no dependencies, None means we could not find out -- a setup.py, or
    a tomllib this interpreter does not have. The caller must not treat the
    second as the first, or it warns about imports the install would satisfy.
    """
    path = os.path.join(project_dir, "pyproject.toml")
    if not os.path.isfile(path):
        return None
    try:
        import tomllib
    except ImportError:  # < 3.11
        return None
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001 - malformed metadata is uv's error to give
        return None
    deps = (data.get("project") or {}).get("dependencies")
    if not isinstance(deps, list):
        return None
    return {_normalize_distribution(d) for d in deps if isinstance(d, str)}


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

    # Where the editable install exists, `-e .` resolves the project's own
    # [project.dependencies] in the same pass as `requirements:`. Warning about
    # those is a false positive, and a false warning about a dependency is worse
    # than none: it teaches the reader to dismiss this check.
    editable = report.capabilities.get("editable_install")
    metadata = any(
        os.path.isfile(os.path.join(project_dir, name))
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )
    unreadable_metadata = False
    if editable and metadata:
        project_deps = _pyproject_dependencies(project_dir)
        if project_deps is None:
            unreadable_metadata = True
        else:
            declared |= project_deps
    base = {_normalize_distribution(item) for item in BASE_AGENT_REQUIREMENTS}
    stdlib = getattr(sys, "stdlib_module_names", frozenset())

    for name, lineno in sorted(toplevel_import_names(tree).items()):
        if name in stdlib or name == "canyonos_core":
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
        if unreadable_metadata:
            mechanism = (
                "The container installs the base list, `requirements:`, and -- "
                "since this project declares packaging metadata -- whatever "
                "`-e .` resolves from it. That metadata could not be read here, "
                f"so if it already requires `{name}` this line is noise; "
                "otherwise it is a ModuleNotFoundError inside _load_agent and "
                "'No agent loaded' on the first request."
            )
        else:
            mechanism = (
                "The container installs the base list plus `requirements:` and "
                "nothing else, so this is a ModuleNotFoundError inside "
                "_load_agent and 'No agent loaded' on the first request. If the "
                f"distribution is named something other than `{name}`, declare "
                f"that name in {report.rel(config_path)}."
            )
        report.warn(
            "W006",
            entrypoint_path,
            lineno,
            f"`import {name}` is in neither the runtime's base list nor this "
            "entry's `requirements:`",
            mechanism,
        )


def _normalize_distribution(name):
    return re.split(r"[<>=!\[;\s]", name.strip().lower(), maxsplit=1)[0].replace(
        "_", "-"
    )


# ------------------------------------------------------------------ #
#  Driver                                                             #
# ------------------------------------------------------------------ #


def validate(project_dir, config_path, capabilities):
    """Inspect only failures hidden behind a successful image build."""
    report = Report(project_dir, capabilities)

    # The build owns config/YAML syntax and shape validation. We read only enough
    # valid structure to locate code for the deeper checks below.
    config, error = load_yaml(config_path)
    if error is not None or not isinstance(config, dict):
        report.unavailable(
            "BUILD",
            "runtime preflight skipped because the config cannot be read; "
            "canyonos build owns and reports this error.",
        )
        return report

    import glob

    yaml_paths = sorted(glob.glob(os.path.join(project_dir, "agents", "*.yaml")))
    report.stub_module_names = {
        os.path.splitext(os.path.basename(path))[0] for path in yaml_paths
    }

    agents_by_name = {}
    stub_classes = {}
    for path in yaml_paths:
        data, yaml_error = load_yaml(path)
        agent = data.get("agent") if isinstance(data, dict) else None
        name = agent.get("name") if isinstance(agent, dict) else None
        if yaml_error is not None or not isinstance(name, str):
            continue  # canyonos build reports malformed agent declarations
        agents_by_name[name] = (path, agent)
        stub_classes[os.path.splitext(os.path.basename(path))[0]] = name

    entries = config.get("agents")
    if not isinstance(entries, list):
        report.unavailable(
            "BUILD",
            "runtime preflight skipped because `agents:` is not a list; "
            "canyonos build owns and reports this error.",
        )
        return report

    entrypoints = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") == "workflow":
            continue
        name = entry.get("name")
        entrypoint = entry.get("entrypoint")
        if isinstance(entrypoint, str):
            entrypoints.append(entrypoint)
        if name in agents_by_name:
            yaml_path, agent_block = agents_by_name[name]
            check_adapter(report, yaml_path, agent_block, entry, project_dir)
        entrypoint_path = os.path.join(project_dir, entrypoint or "")
        if isinstance(entrypoint, str) and os.path.isfile(entrypoint_path):
            check_requirements_coverage(
                report, project_dir, entry, entrypoint_path, config_path
            )

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") != "workflow":
            continue
        workflow_file = entry.get("workflow_file")
        if not isinstance(workflow_file, str):
            continue
        workflow_path = os.path.join(project_dir, workflow_file)
        if os.path.isfile(workflow_path):
            check_workflow(report, workflow_path, stub_classes)

    # These survive a green build and otherwise surface only in a container or
    # on its first request.
    check_flat_collisions(report, project_dir, yaml_paths, entrypoints)
    check_env_file(report, config, config_path, project_dir)

    entrypoint_paths = [
        os.path.join(project_dir, e)
        for e in entrypoints
        if os.path.isfile(os.path.join(project_dir, e))
    ]
    check_import_root(report, project_dir, entrypoint_paths)

    port_paths = list(entrypoint_paths)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("workflow_file"), str):
            candidate = os.path.join(project_dir, entry["workflow_file"])
            if os.path.isfile(candidate):
                port_paths.append(candidate)

    # Secret detection remains because a green image build would permanently
    # bake the credential into every image.
    check_secrets(report, port_paths)
    return report



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
    if not caps.get("canyonos_core"):
        print("canyonos is not importable here -- capability-gated rules are")
        print("reported UNAVAILABLE rather than checked.\n")
    else:
        print("CanyonOS Core capabilities detected:")
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
        description="Check a CanyonOS Core port against the rules in SKILL.md."
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
