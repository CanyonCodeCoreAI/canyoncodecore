"""V006-V010 -- adapter faults the controller swallows inside _load_agent."""

import ast
import builtins
import os

from validation.core import line_of
from validation.python_source import (
    class_methods,
    find_class,
    parameter_names,
    parse_python,
    required_parameters,
)

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
