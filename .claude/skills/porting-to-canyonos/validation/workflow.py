"""V016-V018, V023 -- the workflow module and how it reaches an agent."""

import ast

from validation.python_source import parameter_names, parse_python, required_parameters


def check_stub_imports(report, workflow_path, tree, stub_modules):
    """V023 -- the workflow must import each agent from its own entrypoint module.

    The build writes a stub over exactly one path: the agent's `entrypoint`
    inside the source copy. An import that reaches the class any other way --
    flat, through a package re-export, or from a second copy of the module --
    resolves to the real class instead, and the workflow runs the agent
    in-process with none of the deployment behind it. The class name is another
    trap: the deploy build prints one with a `Stub` suffix that it never writes.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            name = alias.name
            base = name.removesuffix("Stub")
            expected = stub_modules.get(base)
            if expected is None:
                continue
            if name.endswith("Stub"):
                report.error(
                    "V023",
                    workflow_path,
                    node.lineno,
                    f"`{name}` is the name the build prints, not the class it writes",
                    "generate_stub sets class_name = agent_config['name'] and "
                    "then recomputes it with a 'Stub' suffix for the log line "
                    "only. The message names a class that does not exist; the "
                    f"class is `{base}`.",
                )
            elif node.module != expected:
                report.error(
                    "V023",
                    workflow_path,
                    node.lineno,
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
