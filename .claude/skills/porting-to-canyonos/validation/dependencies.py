"""W003, W006 -- credentials and imports a successful build does not reject."""

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
    NAMESPACE_DISTRIBUTIONS,
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
    report,
    project_dir,
    entry,
    root_path,
    config_path,
    base_requirements,
    shadowed_paths=(),
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

    external = reachable_imports(project_dir, root_path, shadowed_paths)
    for dotted, (where, lineno) in sorted(external.items()):
        name = dotted.split(".")[0]
        if name in STDLIB_MODULE_NAMES or name == "ventis":
            continue
        # Provided by the image itself: the shared runtime is copied flat over
        # the swept tree. A stub is not listed here -- it replaces a module the
        # source copy already carries, so the tree checks below cover it.
        if f"{name}.py" in RUNTIME_FLAT_NAMES:
            continue
        if resolves_flat(project_dir, name) or resolves_nested(project_dir, name):
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
        report.error(
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
