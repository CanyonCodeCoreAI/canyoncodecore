"""V021, W003, W006 -- credentials and imports a green build does not reject."""

import ast
import os
import re

from validation.python_source import (
    parse_python,
    reachable_imports,
    resolves_flat,
    resolves_nested,
)
from validation.runtime import (
    IMPORT_TO_DISTRIBUTION,
    RUNTIME_FLAT_NAMES,
    STDLIB_MODULE_NAMES,
)

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "an OpenAI-style secret key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "a Google API key"),
]


SECRET_NAME = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)


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
                    report.error(
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


def check_requirements_coverage(
    report,
    project_dir,
    entry,
    root_path,
    config_path,
    base_requirements,
    shadowed_paths=(),
    flat_stub_names=(),
):
    """W006 -- an import the container cannot satisfy.

    Walks the whole import graph the image executes from `root_path`, not just
    that one file: a distribution reached through a local module or a package
    __init__ is exactly as missing, and exactly as invisible until the container
    starts. `flat_stub_names` are the module names the build writes a stub over
    at the context root, which resolve to generated code with no dependencies
    of their own.
    """
    declared = {
        _normalize_distribution(item)
        for item in (entry.get("requirements") or [])
        if isinstance(item, str)
    }

    base = {_normalize_distribution(item) for item in base_requirements}
    satisfied = base | declared

    if entry.get("type", "agent") == "workflow":
        failure = (
            "an ImportError at container start, leaving the deployment with no "
            "HTTP entry point for its whole life"
        )
    else:
        failure = (
            "a ModuleNotFoundError inside _load_agent and 'No agent loaded' on "
            "the first request"
        )

    external = reachable_imports(project_dir, root_path, shadowed_paths)
    for dotted, (where, lineno) in sorted(external.items()):
        name = dotted.split(".")[0]
        if name in STDLIB_MODULE_NAMES:
            continue
        if name == "canyonos_core":
            if not dotted.startswith("canyonos_core.llm_proxy"):
                report.error(
                    "V021",
                    where,
                    lineno,
                    f"`{dotted}` -- the image's `canyonos_core` package holds "
                    "only `llm_proxy`",
                    "The build copies the runtime modules flat into the image "
                    "(deploy.py, future.py, canyonos_context.py, ...) under an "
                    "`__init__` that exports nothing, so `from canyonos_core "
                    "import deploy` is an ImportError at container start -- "
                    "after a green build and a deploy that reported every "
                    "agent ready. Import the flat module: `from deploy import "
                    "deploy`.",
                )
            continue
        # Provided by the image itself: the shared runtime is copied flat over
        # the swept tree. A stub is not listed here -- it replaces a module the
        # source copy already carries, so the tree checks below cover it.
        if f"{name}.py" in RUNTIME_FLAT_NAMES or name in flat_stub_names:
            continue
        if resolves_flat(project_dir, name) or resolves_nested(project_dir, name):
            continue
        if _candidate_distributions(dotted) & satisfied:
            continue

        trailing = ""
        if os.path.realpath(where) != os.path.realpath(root_path):
            trailing = (
                f" This image never names `{name}` in {report.rel(root_path)}; "
                f"it runs {report.rel(where)} on the way there, and that module "
                "needs it."
            )

        related = sorted(
            item
            for item in satisfied
            if item.startswith(_normalize_distribution(name) + "-")
        )
        if related:
            report.warn(
                "W006",
                where,
                lineno,
                f"`import {dotted}` is spelled by no declared distribution, but "
                f"{', '.join(related)} may provide it",
                "Settle it from the distribution's own metadata rather than its "
                f"name (`pip show -f {related[0]}`, or import it in the built "
                f"image). If nothing installed provides `{name}`, declare the "
                f"distribution that does in {report.rel(config_path)}: "
                f"unresolved, this is {failure}." + trailing,
            )
            continue

        mechanism = (
            "The container installs the base list plus `requirements:` and "
            f"nothing else, so this is {failure}. If the distribution is named "
            f"something other than `{name}`, declare that name in "
            f"{report.rel(config_path)}."
        )
        report.error(
            "W006",
            where,
            lineno,
            f"`import {dotted}` is in neither the runtime's base list nor "
            f"{entry.get('name') or 'this entry'}'s `requirements:`",
            mechanism + trailing,
        )


def _candidate_distributions(dotted):
    """Every distribution name that would satisfy `import <dotted>`.

    A namespace package is normally published as its dotted path joined with
    dashes -- `google.adk` as google-adk, `llama_index.core` as
    llama-index-core -- so those are derived rather than enumerated.
    """
    segments = dotted.split(".")
    candidates = {
        _normalize_distribution(item)
        for item in IMPORT_TO_DISTRIBUTION.get(segments[0], (segments[0],))
    }
    for depth in range(2, len(segments) + 1):
        candidates.add(_normalize_distribution("-".join(segments[:depth])))
    return candidates


def _normalize_distribution(name):
    return re.split(r"[<>=!\[;\s]", name.strip().lower(), maxsplit=1)[0].replace(
        "_", "-"
    )
