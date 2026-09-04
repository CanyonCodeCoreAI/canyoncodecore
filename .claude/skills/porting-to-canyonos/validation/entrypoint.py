"""V019, V020, V033-V035 -- traps set by which module the entrypoint names."""

import ast
import os

from validation.python_source import module_path, parse_python
from validation.runtime import RUNTIME_FLAT_NAMES


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
                f"a project module named `{entry}` sits at the root of the source copy",
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
        hit = (
            target == module
            if node.level
            else target
            in (
                module,
                f"{package}.{module}",
            )
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
