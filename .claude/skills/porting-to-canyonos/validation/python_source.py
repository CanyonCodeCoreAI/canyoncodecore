"""Static Python-source discovery used by adapter and packaging checks."""

import ast
import os


def parse_python(path):
    """Return ``(AST, None)`` or ``(None, error)`` without importing the file."""
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
    """Return every keyword-callable parameter, excluding ``self``/``cls``."""
    args = func_node.args
    positional = [arg.arg for arg in args.posonlyargs + args.args]
    if positional and positional[0] in ("self", "cls"):
        positional = positional[1:]
    return positional + [arg.arg for arg in args.kwonlyargs]


def required_parameters(func_node):
    """Return parameters without defaults, excluding ``self``/``cls``."""
    args = func_node.args
    positional = [arg.arg for arg in args.posonlyargs + args.args]
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
    """Return top-level import names and their first line numbers."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.setdefault(alias.name.split(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.setdefault(node.module.split(".")[0], node.lineno)
    return names


def dotted_import_names(tree):
    """Return absolute dotted imports and their first line numbers."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.setdefault(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.setdefault(node.module, node.lineno)
    return names


def _local_module_files(project_dir, dotted):
    """Return local files executed by importing ``dotted``, outermost first."""
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
    """Return local files executed by a module's relative imports."""
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
        found += [candidate for candidate in candidates if os.path.isfile(candidate)]
    return found


def reachable_imports(project_dir, root_path, shadowed_paths=()):
    """Return third-party imports reachable from ``root_path`` transitively."""
    external = {}
    seen = set()
    shadowed = {os.path.realpath(path) for path in shadowed_paths}
    queue = [os.path.realpath(root_path)]
    while queue:
        path = queue.pop()
        if path in shadowed or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        tree, _ = parse_python(path)
        if tree is None:
            continue
        for dotted, lineno in dotted_import_names(tree).items():
            local = _local_module_files(project_dir, dotted)
            if local:
                queue += [os.path.realpath(item) for item in local]
            else:
                external.setdefault(dotted, (path, lineno))
        queue += [
            os.path.realpath(item)
            for item in _relative_import_files(project_dir, path, tree)
        ]
    return external
