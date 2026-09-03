#!/usr/bin/env python3
"""Preflight the runtime traps that `ventis build` cannot see.

This deliberately does not duplicate build-time validation such as malformed
YAML, missing entrypoints, or config-to-yaml matching. `ventis build` owns those
checks. This script parses Python without importing it and catches failures that
otherwise stay hidden until a container loads an agent, starts a workflow, or
serves its first request. A replica is not evidence: the controller writes
`healthy` to Redis before `_load_agent` runs.

    python validate.py [artifact_root] [-c config/global_controller.yaml]
                       [--json] [--strict]

`artifact_root` is the `.car` directory: `config/` beside `app/`, the copy of
the application source that becomes /app inside every container.

Exit 1 if any ERROR was reported, 0 otherwise. --strict also fails on warnings.

Runtime capabilities vary across CanyonOS Core installations. This script probes
the importable `ventis` package directly. A capability-gated check reports
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
# ventis/cli.py SOURCE_DIR_NAME -- the duplicated application source.
SOURCE_DIR_NAME = "app"

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
    "psycopg",
]
# Import name -> every distribution that provides it. A tuple rather than a
# string because more than one distribution can ship the same import name, and
# reporting a correct declaration as a gap teaches the reader to dismiss W006.
IMPORT_TO_DISTRIBUTION = {
    "attr": ("attrs",),
    # `import autogen` is shipped by three unrelated distributions: pyautogen
    # (Microsoft's original), ag2 (the community continuation), and a package
    # literally named autogen. Any of them satisfies the import.
    "autogen": ("pyautogen", "ag2", "autogen", "autogen-agentchat"),
    "bs4": ("beautifulsoup4",),
    "cv2": ("opencv-python",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "grpc": ("grpcio",),
    "grpc_tools": ("grpcio-tools",),
    "jwt": ("pyjwt",),
    "PIL": ("pillow",),
    "psycopg": ("psycopg",),
    "psycopg2": ("psycopg2-binary",),
    "pydantic_settings": ("pydantic-settings",),
    "sklearn": ("scikit-learn",),
    "typing_extensions": ("typing-extensions",),
    "yaml": ("pyyaml",),
}
# Import name -> distribution prefix, where the top-level package is shipped by
# a family of distributions rather than one. `import llama_index.llms.openai`
# collapses to `llama_index`, which no correctly scoped requirements list ever
# names: it declares llama-index-core, llama-index-llms-openai and so on. Any
# member of the family satisfies the import.
NAMESPACE_DISTRIBUTIONS = {
    "llama_index": "llama-index",
}

def _stdlib_names():
    """Module names the interpreter provides without any distribution.

    `sys.stdlib_module_names` exists only from 3.10. Below that, derive the set
    from the interpreter's own library directory rather than shipping a list
    that rots -- without it every `import os` in the copy becomes a W006, and a
    check that cries wolf is a check the reader stops reading.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    found = set(sys.builtin_module_names)
    library = os.path.dirname(os.__file__)
    try:
        entries = os.listdir(library)
    except OSError:
        return frozenset(found)
    for entry in entries:
        if entry.endswith(".py"):
            found.add(entry[:-3])
        elif "." not in entry and "-" not in entry:
            found.add(entry)
    return frozenset(found)


STDLIB_MODULE_NAMES = _stdlib_names()

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


def dotted_import_names(tree):
    """Every absolute import in the module as its full dotted path, with lines."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.setdefault(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.setdefault(node.module, node.lineno)
    return names


def _local_module_files(project_dir, dotted):
    """Files inside the copy that `import <dotted>` executes, outermost first.

    Python runs every package `__init__.py` on the way down before the leaf
    module. That is how an image ends up executing code it never names: the
    workflow imports `pkg.agent`, `pkg/__init__.py` runs first, and whatever it
    imports runs with it.
    """
    parts = dotted.split(".")
    found = []
    prefix = project_dir
    for depth, part in enumerate(parts):
        if os.path.isdir(os.path.join(prefix, part)):
            init_path = os.path.join(prefix, part, "__init__.py")
            if os.path.isfile(init_path):
                found.append(init_path)
            prefix = os.path.join(prefix, part)
            continue
        leaf = os.path.join(prefix, part + ".py")
        if depth == len(parts) - 1 and os.path.isfile(leaf):
            found.append(leaf)
        return found
    return found


def _relative_import_files(project_dir, path, tree):
    """Files a module's own `from .sibling import x` imports execute."""
    root = os.path.realpath(project_dir)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = os.path.dirname(path)
        for _ in range(node.level - 1):
            base = os.path.dirname(base)
        resolved = os.path.realpath(base)
        if resolved != root and not resolved.startswith(root + os.sep):
            continue
        target = os.path.join(base, *(node.module.split(".") if node.module else []))
        candidates = [target + ".py", os.path.join(target, "__init__.py")]
        candidates += [
            os.path.join(target, alias.name + ".py") for alias in node.names
        ]
        candidates += [
            os.path.join(target, alias.name, "__init__.py") for alias in node.names
        ]
        found += [c for c in candidates if os.path.isfile(c)]
    return found


def reachable_imports(project_dir, root_path):
    """Every third-party import the image executes from `root_path`, transitively.

    An image runs far more than the file the config names. `tools/parser.py` is
    one hop from an entrypoint and its pdfplumber import is invisible to an AST
    walk of the entrypoint alone; a package `__init__.py` three hops up drags in
    graphql-core. Both surface only as a ModuleNotFoundError deep inside an
    import chain at agent load, long after a green build.

    Returns {dotted import name: (file that imports it, line)} for names that do
    not resolve inside the copy.
    """
    external = {}
    seen = set()
    queue = [os.path.realpath(root_path)]
    while queue:
        path = queue.pop()
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        tree, _ = parse_python(path)
        if tree is None:
            continue
        for dotted, lineno in dotted_import_names(tree).items():
            local = _local_module_files(project_dir, dotted)
            if local:
                queue += [os.path.realpath(p) for p in local]
            else:
                external.setdefault(dotted, (path, lineno))
        queue += [
            os.path.realpath(p)
            for p in _relative_import_files(project_dir, path, tree)
        ]
    return external


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


def check_stub_imports(report, workflow_path, tree, stub_modules):
    """V023 -- the workflow must import each agent from its own entrypoint module.

    The build writes a stub over exactly one path: the agent's `entrypoint`
    inside the source copy. An import that reaches the class any other way --
    flat, through a package re-export, or from a second copy of the module --
    resolves to the real class instead, and the workflow runs the agent
    in-process with none of the deployment behind it. The class name is another
    trap: `ventis build` prints one with a `Stub` suffix that it never writes.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            name = alias.name
            base = name[: -len("Stub")] if name.endswith("Stub") else name
            expected = stub_modules.get(base)
            if expected is None:
                continue
            if name.endswith("Stub"):
                report.error(
                    "V023", workflow_path, node.lineno,
                    f"`{name}` is the name the build prints, not the class it "
                    f"writes",
                    "generate_stub sets class_name = agent_config['name'] and "
                    "then recomputes it with a 'Stub' suffix for the log line "
                    "only. The message names a class that does not exist; the "
                    f"class is `{base}`.",
                )
            elif node.module != expected:
                report.error(
                    "V023", workflow_path, node.lineno,
                    f"`from {node.module} import {name}` -- the stub for {name} "
                    f"is written to {expected.replace('.', '/')}.py",
                    "The build replaces the module at the agent's entrypoint "
                    "and nothing else, so this import reaches the real class "
                    "and runs the agent in this process instead of over gRPC. "
                    f"Import it from `{expected}`, where the source already "
                    "keeps it.",
                )


def check_workflow(report, workflow_path, stub_modules=None):
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

    if stub_modules:
        check_stub_imports(report, workflow_path, tree, stub_modules)

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


def check_flat_collisions(report, source_dir, entrypoints):
    """V019 V020 -- later copies land on earlier ones at the context root."""
    for entry in sorted(os.listdir(source_dir)):
        path = os.path.join(source_dir, entry)
        if not os.path.isfile(path) or not entry.endswith(".py"):
            continue

        # V019 -- the shared runtime is copied flat, after the project sweep.
        if entry in RUNTIME_FLAT_NAMES:
            report.error(
                "V019",
                path,
                1,
                f"a project module named `{entry}` sits at the root of the "
                "source copy",
                "The shared CanyonOS Core runtime is copied flat into the image after "
                "the project sweep, so this file is overwritten by CanyonOS Core's own "
                f"{entry}. Rename it or move it into a package directory.",
            )

    # V020 -- one module cannot be the entrypoint of two agents.
    owners = {}
    for name, entrypoint in entrypoints:
        owners.setdefault(entrypoint, []).append(name)
    for entrypoint, names in sorted(owners.items()):
        if len(names) < 2:
            continue
        report.error(
            "V020",
            os.path.join(source_dir, entrypoint),
            1,
            f"{' and '.join(sorted(names))} both declare `{entrypoint}` as their "
            "entrypoint",
            "Each agent's stub is written over its own entrypoint, so the two "
            "land on one path and the last one built wins. Every caller then "
            "reaches whichever agent that was. Give each agent its own module.",
        )


# ------------------------------------------------------------------ #
#  V030-V031  capability-gated rules                                  #
# ------------------------------------------------------------------ #


def check_env_file(report, config, config_path, artifact_dir):
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
                "No resolve_env_file in the importable ventis package, so the "
                "key is silently dropped and the container answers a provider "
                "credential error on the first request. This port requires the "
                "`env_file` runtime capability.",
            )
        else:
            report.unavailable(
                "V030",
                "env_file is not supported by the importable `ventis` runtime. "
                "Credentials have no declared path into a container on this tree.",
            )
        return

    if not declared:
        report.warn(
            "V030",
            config_path,
            line_of(config),
            "no `env_file:` in the config",
            "Only runtime-managed VENTIS_* variables are guaranteed without it. "
            "If the source reads credentials from the environment, the first "
            "request fails on a provider error.",
        )
        return

    # Path existence and readability are deploy-preflight checks. Do not
    # duplicate them here.


def check_import_root(report, source_dir, entrypoint_paths):
    """V031 -- gated on detected editable-install support."""
    supported = report.capabilities.get("editable_install")
    has_metadata = any(
        os.path.isfile(os.path.join(source_dir, name))
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )

    non_flat = []
    for path in entrypoint_paths:
        tree, _ = parse_python(path)
        if tree is None:
            continue
        for name, lineno in toplevel_import_names(tree).items():
            if f"{name}.py" in RUNTIME_FLAT_NAMES:
                continue
            if _resolves_flat(source_dir, name):
                continue
            location = _resolves_nested(source_dir, name)
            if location:
                non_flat.append((path, lineno, name, location))

    if not supported:
        report.unavailable(
            "V031",
            "the editable install (`-e .`) is not supported by the importable "
            "`ventis` runtime. Only names rooted at /app import inside a container.",
        )
        for path, lineno, name, location in non_flat:
            report.error(
                "V031",
                path,
                lineno,
                f"`import {name}` resolves to {location}, which is not at the "
                "root of the source copy",
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
                f"`import {name}` resolves to {location}, and the source copy's "
                "root has no packaging metadata",
                "A pyproject.toml, setup.py or setup.cfg at the root of the "
                "source copy is what adds `-e .`; metadata nested deeper in the "
                "tree is ignored. Add minimal root metadata pointing at the "
                "existing package directory. Without it the install is skipped "
                "silently.",
            )


# ------------------------------------------------------------------ #
#  V033-V035  traps set by where the entrypoint sits                  #
# ------------------------------------------------------------------ #


def check_entrypoint_module(report, source_dir, name, entrypoint):
    """V033 V034 V035.

    Two runtime facts collide here. The build writes this agent's stub over
    `entrypoint` in every image except this agent's own, and the controller
    loads the real file by path rather than by import. Each breaks a module
    layout that is correct everywhere else in Python.
    """
    path = os.path.join(source_dir, entrypoint)
    if not os.path.isfile(path):
        return

    segments = os.path.splitext(entrypoint)[0].replace("\\", "/").split("/")
    invalid = [part for part in segments if not part.isidentifier()]
    if invalid:
        report.error(
            "V034",
            path,
            0,
            f"`{invalid[0]}` in the entrypoint path is not a Python identifier",
            "The controller loads the entrypoint by file path, so this file runs "
            "-- but the workflow has to import the class from "
            f"`{module_path(entrypoint)}` (V023), and that is a SyntaxError, not "
            "an ImportError. Rename the file inside the copy, or point "
            "`entrypoint` at a normally-named sibling that loads this file by "
            "path and re-exposes the class.",
        )

    tree, _ = parse_python(path)
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                spelling = "." * node.level + (node.module or "")
                report.error(
                    "V035",
                    path,
                    node.lineno,
                    f"the entrypoint's own `from {spelling} import ...` is relative",
                    "_load_agent loads this file with spec_from_file_location("
                    "VENTIS_AGENT_FILE.replace('.py', ''), path). That name keeps "
                    "the entrypoint's directory separator, so it has no parent "
                    "package and __package__ is empty: every relative import in "
                    "this file raises 'attempted relative import with no known "
                    "parent package' at agent load, behind 'No agent loaded'. "
                    "Make this file's own top-level imports absolute; modules it "
                    "imports may keep theirs.",
                )
                break

    directory = os.path.dirname(entrypoint)
    if not directory:
        return
    init_path = os.path.join(source_dir, directory, "__init__.py")
    if not os.path.isfile(init_path):
        return
    module = os.path.splitext(os.path.basename(entrypoint))[0]
    package = directory.replace("\\", "/").replace("/", ".")
    init_tree, _ = parse_python(init_path)
    if init_tree is None:
        return
    for node in ast.walk(init_tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = node.module or ""
        hit = target == module if node.level else target in (
            module,
            f"{package}.{module}",
        )
        if not hit and node.level and not node.module:
            hit = any(alias.name == module for alias in node.names)
        if not hit:
            continue
        report.error(
            "V033",
            init_path,
            node.lineno,
            f"`{package}/__init__.py` re-exports from `{module}`, the entrypoint "
            f"for {name}",
            "Python runs a package's __init__.py before any of its submodules, "
            "and in every image except this agent's own the module at the "
            "entrypoint is the generated stub, which defines the agent class and "
            f"nothing else. Any peer image that imports anything from `{package}` "
            "-- the workflow importing the agent class included -- re-runs this "
            "re-export against the stub and dies at container startup with "
            f"ImportError. Point `entrypoint` at a module `{package}/__init__.py` "
            "does not re-export from; add one that imports the real module if "
            "every existing module is re-exported.",
        )
        break


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
    report, project_dir, entry, root_path, config_path, base_requirements
):
    """W006 -- an import the container cannot satisfy.

    Walks the whole import graph the image executes from `root_path`, not just
    that one file: a distribution reached through a local module or a package
    __init__ is exactly as missing, and exactly as invisible until the container
    starts.
    """
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
    base = {_normalize_distribution(item) for item in base_requirements}
    satisfied = base | declared

    external = reachable_imports(project_dir, root_path)
    for dotted, (where, lineno) in sorted(external.items()):
        name = dotted.split(".")[0]
        if name in STDLIB_MODULE_NAMES or name == "ventis":
            continue
        # Provided by the image itself: the shared runtime is copied flat over
        # the swept tree. A stub is not listed here -- it replaces a module the
        # source copy already carries, so the tree checks below cover it.
        if f"{name}.py" in RUNTIME_FLAT_NAMES:
            continue
        if _resolves_flat(project_dir, name) or _resolves_nested(project_dir, name):
            continue
        prefix = NAMESPACE_DISTRIBUTIONS.get(name)
        if prefix and any(
            item == prefix or item.startswith(prefix + "-") for item in declared
        ):
            continue
        if _candidate_distributions(name) & satisfied:
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
        if os.path.realpath(where) != os.path.realpath(root_path):
            mechanism += (
                f" This image never names `{name}` in {report.rel(root_path)}; "
                f"it runs {report.rel(where)} on the way there, and that module "
                "needs it."
            )
        report.warn(
            "W006",
            where,
            lineno,
            f"`import {dotted}` is in neither the runtime's base list nor "
            f"{entry.get('name') or 'this entry'}'s `requirements:`",
            mechanism,
        )


def _candidate_distributions(name):
    """Every distribution name that would satisfy `import <name>`."""
    return {
        _normalize_distribution(item)
        for item in IMPORT_TO_DISTRIBUTION.get(name, (name,))
    }


def _normalize_distribution(name):
    return re.split(r"[<>=!\[;\s]", name.strip().lower(), maxsplit=1)[0].replace(
        "_", "-"
    )


# ------------------------------------------------------------------ #
#  Driver                                                             #
# ------------------------------------------------------------------ #


def find_agent_declarations(config_dir):
    """Map agent name -> declaration, for every declaration in `config/`.

    Mirrors ventis/cli.py: declarations sit in `config/` beside the manifest,
    which -- like `policy.yaml` -- carries no top-level `agent.name` and so
    drops out here.
    """
    import glob

    declarations = {}
    for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        data, error = load_yaml(path)
        if error is not None or not isinstance(data, dict):
            continue
        agent = data.get("agent")
        name = agent.get("name") if isinstance(agent, dict) else None
        if isinstance(name, str) and name:
            declarations[name] = (path, agent)
    return declarations


def module_path(entrypoint):
    """Dotted module name an entrypoint has inside the container."""
    return os.path.splitext(entrypoint)[0].replace("\\", "/").replace("/", ".")


def validate(artifact_dir, config_path, capabilities):
    """Inspect only failures hidden behind a successful image build."""
    report = Report(artifact_dir, capabilities)

    # The build owns config/YAML syntax and shape validation. We read only enough
    # valid structure to locate code for the deeper checks below.
    config, error = load_yaml(config_path)
    if error is not None or not isinstance(config, dict):
        report.unavailable(
            "BUILD",
            "runtime preflight skipped because the config cannot be read; "
            "ventis build owns and reports this error.",
        )
        return report

    source_dir = os.path.join(artifact_dir, SOURCE_DIR_NAME)
    if not os.path.isdir(source_dir):
        report.error(
            "V032",
            artifact_dir,
            0,
            f"no `{SOURCE_DIR_NAME}/` beside `config/`",
            "The artifact root holds the application source it deploys: "
            f"`{SOURCE_DIR_NAME}/` is the copy that becomes /app, and every "
            "entrypoint is relative to it. Without it the port has nothing to "
            "build and nothing to keep it decoupled from the developer's tree.",
        )
        return report

    agents_by_name = find_agent_declarations(os.path.dirname(config_path))

    entries = config.get("agents")
    if not isinstance(entries, list):
        report.unavailable(
            "BUILD",
            "runtime preflight skipped because `agents:` is not a list; "
            "ventis build owns and reports this error.",
        )
        return report

    entrypoints = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") == "workflow":
            continue
        name = entry.get("name")
        entrypoint = entry.get("entrypoint")
        if isinstance(entrypoint, str):
            entrypoints.append((name, entrypoint))
        if name in agents_by_name:
            yaml_path, agent_block = agents_by_name[name]
            check_adapter(report, yaml_path, agent_block, entry, source_dir)
        entrypoint_path = os.path.join(source_dir, entrypoint or "")
        if isinstance(entrypoint, str) and os.path.isfile(entrypoint_path):
            check_entrypoint_module(report, source_dir, name, entrypoint)
            check_requirements_coverage(
                report,
                source_dir,
                entry,
                entrypoint_path,
                config_path,
                BASE_AGENT_REQUIREMENTS,
            )

    # Where each agent's stub is written, and therefore the only import that
    # reaches it over gRPC.
    stub_modules = {
        name: module_path(entrypoint)
        for name, entrypoint in entrypoints
        if name in agents_by_name
    }

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type", "agent") != "workflow":
            continue
        workflow_file = entry.get("workflow_file")
        if not isinstance(workflow_file, str):
            continue
        workflow_path = os.path.join(source_dir, workflow_file)
        if os.path.isfile(workflow_path):
            check_workflow(report, workflow_path, stub_modules)
            # The workflow image installs its own list. A module it imports for
            # a helper drags that module's dependencies in even though the
            # workflow makes no model call of its own.
            check_requirements_coverage(
                report,
                source_dir,
                entry,
                workflow_path,
                config_path,
                BASE_WORKFLOW_REQUIREMENTS,
            )

    # These survive a green build and otherwise surface only in a container or
    # on its first request.
    check_flat_collisions(report, source_dir, entrypoints)
    check_env_file(report, config, config_path, artifact_dir)

    entrypoint_paths = [
        os.path.join(source_dir, e)
        for _, e in entrypoints
        if os.path.isfile(os.path.join(source_dir, e))
    ]
    check_import_root(report, source_dir, entrypoint_paths)

    port_paths = list(entrypoint_paths)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("workflow_file"), str):
            candidate = os.path.join(source_dir, entry["workflow_file"])
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


def print_report(report, artifact_root):
    caps = report.capabilities
    if not caps.get("ventis"):
        print("ventis is not importable here -- capability-gated rules are")
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
        print(f"{artifact_root}: clean.")
        return
    print(f"{errors} error(s), {warnings} warning(s).")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check a CanyonOS Core port against the rules in SKILL.md."
    )
    parser.add_argument(
        "artifact_root",
        nargs="?",
        default=".",
        help="the .car directory holding config/ and app/ (default: the cwd)",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path relative to artifact_root (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--strict", action="store_true", help="fail on warnings as well as errors"
    )
    args = parser.parse_args(argv)

    artifact_root = os.path.abspath(args.artifact_root)
    config_path = (
        args.config
        if os.path.isabs(args.config)
        else os.path.join(artifact_root, args.config)
    )

    capabilities = probe_capabilities()
    report = validate(artifact_root, config_path, capabilities)
    errors, warnings = report.counts()

    if args.json:
        print(
            json.dumps(
                {
                    "artifact_root": artifact_root,
                    "capabilities": capabilities,
                    "errors": errors,
                    "warnings": warnings,
                    "findings": report.findings,
                },
                indent=2,
            )
        )
    else:
        print_report(report, report.rel(artifact_root) or artifact_root)

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
