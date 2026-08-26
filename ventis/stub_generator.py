"""
Stub generator for Ventis agents.

Reads a YAML agent definition and generates an importable Python stub file
where each function returns a Future object. Similar in spirit to how gRPC
generates *_pb2_grpc.py stub files from .proto definitions.


Usage:
    python stub_generator.py <yaml_path> [-o output_path]
"""

import argparse
import ast
import os
import shutil
import yaml

# Packages every agent container needs regardless of its specific business logic.
# grpcio-tools/pyyaml/ipdb/ipython aren't needed/used, but keeping to keep the scope constrained right now
#     - Leave a comment if you want me to remove these, I kept them in since you originally had them but they aren't used
BASE_AGENT_REQUIREMENTS = ["grpcio", "grpcio-tools", "redis", "pyyaml", "psutil", "ipdb", "ipython", "boto3"]

# Workflow will always require these
BASE_WORKFLOW_REQUIREMENTS = BASE_AGENT_REQUIREMENTS + ["flask", "sqlalchemy", "psycopg[binary]"]


def _build_import_nodes():
    """Build import statements for the generated stub module."""
    return [
        ast.ImportFrom(
            module="future",
            names=[ast.alias(name="Future")],
            level=0,
        ),
        ast.Import(names=[ast.alias(name="inspect")]),
    ]


def _build_stub_method(func_config, agent_name):
    """
    Build an AST node for a single stub method.

    Given a function config like:
        name: get_stock_price
        description: Get the stock price for a given ticker.
          - name: ticker
            type: str
        arguments:
        returns:
          type: float

    Generates:
        def get_stock_price(self, ticker: str) -> Future:
            \"\"\"Get the stock price for a given ticker.\"\"\"
            args = {"ticker": ticker.id if isinstance(ticker, Future) else ticker}
            return Future(parent=inspect.stack()[1].filename, service="FinanceAgent",
                          method="get_stock_price", args=args, grpc_stub=self.stub)
    """
    func_name = func_config["name"]
    description = func_config.get("description", "")
    arguments = func_config.get("arguments", [])

    # Build argument nodes: self + declared args with type annotations
    args_list = [ast.arg(arg="self")]
    for arg in arguments:
        arg_node = ast.arg(
            arg=arg["name"],
            annotation=ast.Name(id=arg["type"]) if "type" in arg else None,
        )
        args_list.append(arg_node)

    func_args = ast.arguments(
        posonlyargs=[],
        args=args_list,
        vararg=None,
        kwonlyargs=[],
        kw_defaults=[],
        kwarg=None,
        defaults=[],
    )

    # Build the function body
    body = []

    # Docstring
    if description:
        body.append(ast.Expr(value=ast.Constant(value=description)))

    # Build the args dict with Future replacement:
    # args = {"ticker": ticker.id if isinstance(ticker, Future) else ticker, ...}
    arg_dict_keys = [ast.Constant(value=a["name"]) for a in arguments]
    arg_dict_values = []
    for a in arguments:
        # value.id if isinstance(value, Future) else value
        arg_dict_values.append(
            ast.IfExp(
                test=ast.Call(
                    func=ast.Name(id="isinstance"),
                    args=[ast.Name(id=a["name"]), ast.Name(id="Future")],
                    keywords=[],
                ),
                body=ast.Attribute(value=ast.Name(id=a["name"]), attr="id"),
                orelse=ast.Name(id=a["name"]),
            )
        )

    # args = {"ticker": ticker.id if isinstance(ticker, Future) else ticker, ...}
    body.append(
        ast.Assign(
            targets=[ast.Name(id="args")],
            value=ast.Dict(keys=arg_dict_keys, values=arg_dict_values),
            lineno=0,
        )
    )

    # return Future(parent=..., service=..., method=..., args=args)
    body.append(
        ast.Return(
            value=ast.Call(
                func=ast.Name(id="Future"),
                args=[],
                keywords=[
                    ast.keyword(
                        arg="parent",
                        value=ast.Attribute(
                            value=ast.Subscript(
                                value=ast.Call(
                                    func=ast.Attribute(
                                        value=ast.Name(id="inspect"),
                                        attr="stack",
                                    ),
                                    args=[],
                                    keywords=[],
                                ),
                                slice=ast.Constant(value=1),
                            ),
                            attr="filename",
                        ),
                    ),
                    ast.keyword(
                        arg="service",
                        value=ast.Constant(value=agent_name),
                    ),
                    ast.keyword(
                        arg="method",
                        value=ast.Constant(value=func_name),
                    ),
                    ast.keyword(
                        arg="args",
                        value=ast.Name(id="args"),
                    ),
                ],
            ),
        )
    )

    # Build the function def with -> Future return annotation
    func_def = ast.FunctionDef(
        name=func_name,
        args=func_args,
        body=body,
        decorator_list=[],
        returns=ast.Name(id="Future"),
    )

    return func_def


def _build_stub_class(agent_config):
    """
    Build an AST node for the entire stub class.

    Generates a class like:
        class FinanceAgentStub(object):
            def __init__(self):
                pass
            ...stub methods...
    """
    # class_name = agent_config["name"] + "Stub"
    class_name = agent_config["name"] 
    functions = agent_config.get("functions", [])

    # __init__ method: simple pass, no gRPC setup needed.
    # Future handles its own gRPC connections via env vars.
    init_method = ast.FunctionDef(
        name="__init__",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="self")],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=[ast.Pass()],
        decorator_list=[],
        returns=None,
    )

    # Build all stub methods
    methods = [init_method]
    for func_config in functions:
        methods.append(_build_stub_method(func_config, agent_config["name"]))

    class_def = ast.ClassDef(
        name=class_name,
        bases=[ast.Name(id="object")],
        keywords=[],
        body=methods,
        decorator_list=[],
    )

    return class_def


def generate_stub(yaml_path, output_path):
    """
    Read a YAML agent definition and generate an importable Python stub file.
    """
    with open(yaml_path, "r") as f:
        config = yaml.safe_load(f)

    agent_config = config["agent"]

    # Build the full module AST
    module = ast.Module(
        body=[
            *_build_import_nodes(),
            _build_stub_class(agent_config),
        ],
        type_ignores=[],
    )

    # Fix missing line numbers required by compile/unparse
    ast.fix_missing_locations(module)

    # Unparse the AST into clean Python source
    source = ast.unparse(module)

    # Use black-style formatting if available, otherwise do basic formatting
    # Add blank lines between methods for readability
    source = _format_source(source)

    # Ensure the output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    with open(output_path, "w") as f:
        f.write(source)

    class_name = agent_config["name"] + "Stub"
    print(f"Generated stub class '{class_name}' -> {output_path}")
    return source


def _format_source(source):
    """Apply basic formatting to make the generated source more readable."""
    lines = source.split("\n")
    formatted = []
    for i, line in enumerate(lines):
        formatted.append(line)
        # Add blank line after import statements
        if line.startswith("from ") or line.startswith("import "):
            formatted.append("")
        # Add blank line before method definitions (except first in class)
        if i + 1 < len(lines) and lines[i + 1].strip().startswith("def "):
            if not line.strip().startswith("class "):
                formatted.append("")

    return "\n".join(formatted) + "\n"


# Directories ventis build itself generates inside a project -- never swept.
_GENERATED_DIRS = {"docker_container", "stubs", "grpc_stubs", "__pycache__"}

# Written into the context by the generator itself; a project file of the same
# name at the root would land on top of it.
_GENERATED_ROOT_FILES = {"requirements.txt", "Dockerfile"}


def _sweep_project_files(project_dir):
    """Recursively collect (abs_src, rel_dst) for every project file, preserving its directory structure.

    Not only modules: the editable install below reads the project's packaging
    metadata, and that metadata routinely points at a README or a license file,
    so a sweep that took `.py` alone would leave nothing installable. Hidden
    files are skipped -- `.env` holds credentials and has no business in an
    image.
    """
    swept = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _GENERATED_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(root, project_dir)
        at_root = rel_dir == os.curdir
        for fname in files:
            if fname.startswith("."):
                continue
            if at_root and fname in _GENERATED_ROOT_FILES:
                continue
            abs_src = os.path.join(root, fname)
            rel_dst = os.path.relpath(abs_src, project_dir)
            swept.append((abs_src, rel_dst))
    return swept


def _stub_destinations(stub_file, project_dir):
    """Every path a stub has to land on.

    Flat is the one that matters: both the agent and the workflow are started as
    `python <basename>.py` from the context root, so `sys.path[0]` is the root
    and a stub anywhere else cannot be imported. When the project tree has been
    swept in, the stub also goes to agents/<basename> to overwrite the real
    implementation the sweep copied there -- a caller that reaches another agent
    must get the stub, not that agent's own class.
    """
    basename = os.path.basename(stub_file)
    if not project_dir:
        return [basename]
    return [basename, os.path.join("agents", basename)]


# What a project must have at its root for `pip install -e .` to mean anything.
_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def _install_step(project_dir):
    """The Dockerfile lines that install requirements, plus the project itself.

    Sweeping the tree in is not enough to make it importable: the process starts
    at the context root, so sys.path[0] is /app and only modules sitting there
    resolve -- a src/ layout resolves to nothing. `-e .` hands the import root to
    the project's own packaging metadata, so Ventis never has to guess a
    directory name. A project that declares no metadata gets the plain install.
    """
    installable = project_dir and any(
        os.path.isfile(os.path.join(project_dir, name)) for name in _PACKAGING_FILES
    )
    if not installable:
        return (
            "COPY requirements.txt .\n"
            "RUN --mount=type=cache,target=/root/.cache/uv "
            "uv pip install --system -r requirements.txt\n"
            "\n"
            "COPY . .\n"
        )
    # The project has to be in the context before it can be installed, so the
    # copy moves ahead of the install and both resolve in one pass.
    return (
        "COPY . .\n"
        "RUN --mount=type=cache,target=/root/.cache/uv "
        "uv pip install --system -r requirements.txt -e .\n"
    )


def generate_docker(
    yaml_path,
    agent_file,
    output_dir=None,
    grpc_stubs_dir=None,
    stub_files=None,
    requirements=None,
    project_dir=None,
):
    """
    Generate a minimal Docker build context for an agent.

    Creates a directory containing a Dockerfile, requirements.txt, and all
    source files needed to run the agent with its own local controller.

    Args:
        yaml_path:      Path to the YAML agent definition.
        agent_file:     Path to the original Python agent implementation.
        output_dir:     Optional output directory (default: docker_container/<AgentName>/).
        grpc_stubs_dir: Optional path to compiled gRPC stubs (default: <repo_root>/grpc_stubs).
        stub_files:     Optional list of agent stub files to copy into the context.
        requirements:   Optional list of extra pip packages this agent needs.
        project_dir:    Optional project root to sweep for extra .py helper files.
    """
    with open(yaml_path, "r") as f:
        config = yaml.safe_load(f)

    agent_name = config["agent"]["name"]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(script_dir, "..")

    if output_dir is None:
        output_dir = os.path.join(project_root, "docker_container", agent_name)

    if grpc_stubs_dir is None:
        grpc_stubs_dir = os.path.join(project_root, "grpc_stubs")

    os.makedirs(output_dir, exist_ok=True)

    # ---- requirements.txt ------------------------------------------------
    # Base packages the shared framework files need, plus this agent's own.
    requirements_txt = "\n".join(BASE_AGENT_REQUIREMENTS + list(requirements or [])) + "\n"
    with open(os.path.join(output_dir, "requirements.txt"), "w") as f:
        f.write(requirements_txt)

    # Sweep the project for extra .py helper files not on the explicit list below.
    files_to_copy = []
    if project_dir:
        files_to_copy += _sweep_project_files(project_dir)

    # Copy general agent files
    files_to_copy += [
        # (source_path, destination_filename)
        (os.path.join(script_dir, "future.py"), "future.py"),
        (os.path.join(script_dir, "ventis_context.py"), "ventis_context.py"),
        (
            os.path.join(script_dir, "controller", "local_controller.py"),
            "local_controller.py",
        ),
        (
            os.path.join(script_dir, "controller", "local_controller_frontend.py"),
            "local_controller_frontend.py",
        ),
        (os.path.join(script_dir, "utils", "redis_client.py"), "redis_client.py"),
        (
            os.path.join(script_dir, "controller", "utils", "gpu_metrics.py"),
            "gpu_metrics.py",
        ),
        (os.path.join(script_dir, "llm", "bedrock.py"), "bedrock.py"),
    ]

    # Copy provided agent stubs, overwriting the swept real file at the same path
    if stub_files:
        for stub_file in stub_files:
            for dst in _stub_destinations(stub_file, project_dir):
                files_to_copy.append((os.path.abspath(stub_file), dst))

    files_to_copy.append((os.path.abspath(agent_file), os.path.basename(agent_file)))

    # Copy gRPC generated stubs if they exist
    if os.path.isdir(grpc_stubs_dir):
        for fname in os.listdir(grpc_stubs_dir):
            if fname.endswith(".py"):
                files_to_copy.append((os.path.join(grpc_stubs_dir, fname), fname))

    for src, dst in files_to_copy:
        if os.path.isfile(src):
            dest_path = os.path.join(output_dir, dst)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(src, dest_path)
        else:
            print(f"  Warning: source file not found, skipping: {src}")

    # Copy the YAML definition too
    shutil.copy2(
        os.path.abspath(yaml_path),
        os.path.join(output_dir, os.path.basename(yaml_path)),
    )

    # ---- Dockerfile ------------------------------------------------------
    agent_basename = os.path.basename(agent_file)
    install_step = _install_step(project_dir)
    dockerfile = f"""# syntax=docker/dockerfile:1
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

{install_step}
ENV VENTIS_AGENT_NAME={agent_name}
ENV VENTIS_AGENT_FILE={agent_basename}

EXPOSE 50051

CMD ["python", "local_controller.py", "--port", "50051"]
"""
    with open(os.path.join(output_dir, "Dockerfile"), "w") as f:
        f.write(dockerfile)

    print(f"Generated Docker context for '{agent_name}' -> {output_dir}")
    return output_dir


def generate_workflow_docker(
    workflow_file,
    stub_files,
    output_dir=None,
    grpc_stubs_dir=None,
    api_port=8080,
    requirements=None,
    project_dir=None,
):
    """
    Generate a Docker build context for a workflow.

    Creates a directory containing a Dockerfile, requirements.txt,
    workflow_launcher.py, and all source files needed to run the workflow
    with its own local controller.

    Args:
        workflow_file:  Path to the workflow Python file.
        stub_files:     List of stub file paths to include.
        output_dir:     Optional output directory (default: docker_container/Workflow/).
        grpc_stubs_dir: Optional path to compiled gRPC stubs (default: <repo_root>/grpc_stubs).
        requirements:   Optional list of extra pip packages this workflow needs.
        project_dir:    Optional project root to sweep for extra .py helper files.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(script_dir, "..")

    if output_dir is None:
        output_dir = os.path.join(project_root, "docker_container", "Workflow")

    if grpc_stubs_dir is None:
        grpc_stubs_dir = os.path.join(project_root, "grpc_stubs")

    os.makedirs(output_dir, exist_ok=True)

    # ---- requirements.txt ------------------------------------------------
    # Base packages the shared framework files need, plus this workflow's own.
    requirements_txt = "\n".join(BASE_WORKFLOW_REQUIREMENTS + list(requirements or [])) + "\n"
    with open(os.path.join(output_dir, "requirements.txt"), "w") as f:
        f.write(requirements_txt)

    # ---- Copy source files into the build context ------------------------
    workflow_basename = os.path.basename(workflow_file)

    # Sweep the project for extra .py helper files not on the explicit list below.
    files_to_copy = []
    if project_dir:
        files_to_copy += _sweep_project_files(project_dir)

    files_to_copy += [
        (os.path.abspath(workflow_file), workflow_basename),
        (os.path.join(script_dir, "future.py"), "future.py"),
        (os.path.join(script_dir, "ventis_context.py"), "ventis_context.py"),
        (os.path.join(script_dir, "deploy.py"), "deploy.py"),
        (
            os.path.join(script_dir, "controller", "local_controller.py"),
            "local_controller.py",
        ),
        (
            os.path.join(script_dir, "controller", "local_controller_frontend.py"),
            "local_controller_frontend.py",
        ),
        (os.path.join(script_dir, "utils", "redis_client.py"), "redis_client.py"),
        *[
            (os.path.join(script_dir, "controller", "utils", name), name)
            for name in ("gpu_metrics.py", "session_logging.py")
        ],
    ]

    # Copy stub files, overwriting the swept real file at the same path
    for stub_file in stub_files:
        for dst in _stub_destinations(stub_file, project_dir):
            files_to_copy.append((os.path.abspath(stub_file), dst))

    # Copy gRPC generated stubs if they exist
    if os.path.isdir(grpc_stubs_dir):
        for fname in os.listdir(grpc_stubs_dir):
            if fname.endswith(".py"):
                files_to_copy.append((os.path.join(grpc_stubs_dir, fname), fname))

    for src, dst in files_to_copy:
        if os.path.isfile(src):
            dest_path = os.path.join(output_dir, dst)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(src, dest_path)
        else:
            print(f"  Warning: source file not found, skipping: {src}")

    # ---- workflow_launcher.py --------------------------------------------
    launcher = f"""import threading
import time
import sys

from local_controller import LocalController


def start_lc():
    controller = LocalController(port=50051)
    controller.run()


# Start local controller in background thread
lc_thread = threading.Thread(target=start_lc, daemon=True)
lc_thread.start()

# Give the LC a moment to start up
time.sleep(1)

# Run the workflow (which calls deploy() -> Flask server)
exec(open("{workflow_basename}").read())
"""
    with open(os.path.join(output_dir, "workflow_launcher.py"), "w") as f:
        f.write(launcher)

    # ---- Dockerfile ------------------------------------------------------
    install_step = _install_step(project_dir)
    dockerfile = f"""# syntax=docker/dockerfile:1
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

{install_step}
EXPOSE 50051
EXPOSE {api_port}

CMD ["python", "workflow_launcher.py"]
"""
    with open(os.path.join(output_dir, "Dockerfile"), "w") as f:
        f.write(dockerfile)

    print(f"Generated workflow Docker context -> {output_dir}")
    return output_dir


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(script_dir, "..")
    stubs_dir = os.path.join(project_root, "stubs")

    parser = argparse.ArgumentParser(
        description="Generate Future-returning stub classes from YAML agent definitions."
    )
    parser.add_argument(
        "yaml_path",
        nargs="?",
        default=os.path.join(project_root, "examples", "finance_agent.yaml"),
        help="Path to the YAML agent definition file (default: examples/finance_agent.yaml)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output path for the generated stub file (default: stubs/<name>_stub.py)",
    )
    parser.add_argument(
        "--agent-file",
        default=None,
        help="Path to the original Python agent file (required for --docker)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Generate a Docker build context for the agent",
    )
    parser.add_argument(
        "--workflow",
        action="store_true",
        help="Generate a Docker build context for a workflow",
    )
    parser.add_argument(
        "--workflow-file",
        default=None,
        help="Path to the workflow Python file (required for --workflow)",
    )
    parser.add_argument(
        "--stub-files",
        nargs="*",
        default=[],
        help="Stub files to include in the workflow Docker context",
    )

    args = parser.parse_args()

    # Always generate the stub (unless --workflow mode)
    if not args.workflow:
        if args.output:
            output_path = args.output
        else:
            base_name = os.path.splitext(os.path.basename(args.yaml_path))[0]
            output_path = os.path.join(stubs_dir, f"{base_name}.py")

        generate_stub(args.yaml_path, output_path)

    # Optionally generate Docker context
    if args.docker:
        if not args.agent_file:
            parser.error("--agent-file is required when using --docker")
        generate_docker(args.yaml_path, args.agent_file, stub_files=args.stub_files)

    # Generate workflow Docker context
    if args.workflow:
        if not args.workflow_file:
            parser.error("--workflow-file is required when using --workflow")
        generate_workflow_docker(args.workflow_file, args.stub_files)
