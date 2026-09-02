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


# What the sweep leaves out. These lists are hardcoded with no project override
# because the build context is assembled by hand, so Docker's own ignore
# mechanism never gets to run.

# Directories ventis build itself generates inside a project -- never swept.
_GENERATED_DIRS = {"docker_container", "stubs", "grpc_stubs"}

# A macOS virtualenv or cache is dead weight in a linux image, at best.
_SKIPPED_DIRS = {"__pycache__", "node_modules", "venv", "site-packages"}

# The generator writes these into the context itself, requirements.txt before
# the copy runs -- a project file of the same name at the root would win.
_RESERVED_CONTEXT_NAMES = {
    "Dockerfile",
    "requirements.txt",
    "workflow_launcher.py",
}

_SKIPPED_SUFFIXES = (".pyc", ".pyo", ".pyd")

# Binary keystores carry no armor to detect them by.
_KEYSTORE_SUFFIXES = (".p12", ".pfx", ".jks")

# Enough to see PEM armor past any header the tool that wrote it left.
_KEY_ARMOR_SCAN_BYTES = 4096

# Past this, say so: usually a dataset, a checkpoint, or a virtualenv under a
# name _SKIPPED_DIRS does not know.
_LARGE_CONTEXT_BYTES = 100 * 1024 * 1024

# Hidden paths every repo has. Held back like all hidden paths, but not worth
# saying so on every build: a note that always fires is wallpaper, and it takes
# the one that matters down with it. Guessing wrong here costs a line of output,
# not a missing file -- which is why the guess lives here and not above.
_UNREMARKABLE_HIDDEN = {
    ".git",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".dockerignore",
    ".editorconfig",
    ".python-version",
    ".venv",
    ".idea",
    ".vscode",
    ".DS_Store",
}


def _worth_reporting(name):
    """Whether a hidden path is project content rather than tooling furniture."""
    return name not in _UNREMARKABLE_HIDDEN and not name.endswith("_cache")


def _looks_like_private_key(path, fname):
    """Whether this file is private key material that must not be baked into an image.

    Matching on names is theater -- an OpenSSH key is called `id_rsa`, with no
    extension at all -- so PEM material is found by its armor instead. A
    certificate is public and ships; only a PRIVATE KEY block is held back.

    Not a secret scanner: `credentials.json` still ships, because nothing can
    recognize it. Credentials belong in the container environment either way.
    """
    if fname.endswith(_KEYSTORE_SUFFIXES):
        return True
    try:
        with open(path, "rb") as handle:
            head = handle.read(_KEY_ARMOR_SCAN_BYTES)
    except OSError:
        return False
    return b"-----BEGIN" in head and b"PRIVATE KEY-----" in head


def _sample(paths, limit=5):
    """Up to `limit` of these paths, with a count standing in for the rest."""
    shown = ", ".join(sorted(paths)[:limit])
    if len(paths) > limit:
        shown += f", (+{len(paths) - limit} more)"
    return shown


def _sweep_project_files(project_dir, exclude_dir=None):
    """Recursively collect (abs_src, rel_dst) for every project file under project_dir, preserving its directory structure.

    Not only .py: source opens PDFs, prompts, and framework config at runtime,
    and a file missing from the image fails only once the agent is serving.

    Every exclusion is reported except the ones that could never have held
    shippable content -- __pycache__, bytecode, symlinks, and the build's own
    output. A file that silently fails to arrive is the bug this sweep exists to
    fix, so dropping one quietly just moves it somewhere harder to find.

    `exclude_dir` is the build context being assembled, which sits under the
    project root. Matching it by resolved path rather than name is what keeps
    this working for a caller that picks an output directory outside
    `_GENERATED_DIRS`.
    """
    swept = []
    hidden = []
    host_local = []
    private_keys = []
    reserved = []
    total_bytes = 0
    largest = (0, None)
    context_dir = os.path.realpath(exclude_dir) if exclude_dir else None

    for root, dirs, files in os.walk(project_dir):
        at_root = root == project_dir

        kept_dirs = []
        for name in dirs:
            if context_dir and os.path.realpath(os.path.join(root, name)) == context_dir:
                continue
            rel_dir = os.path.relpath(os.path.join(root, name), project_dir) + os.sep
            if name.startswith("."):
                if _worth_reporting(name):
                    hidden.append(rel_dir)
            elif name in _SKIPPED_DIRS or name.endswith(".egg-info"):
                if name != "__pycache__":
                    host_local.append(rel_dir)
            elif not (at_root and name in _GENERATED_DIRS):
                kept_dirs.append(name)
        dirs[:] = kept_dirs

        for fname in files:
            abs_src = os.path.join(root, fname)
            rel_dst = os.path.relpath(abs_src, project_dir)
            if fname.startswith("."):
                if _worth_reporting(fname):
                    hidden.append(rel_dst)
                continue
            if os.path.islink(abs_src) or fname.endswith(_SKIPPED_SUFFIXES):
                continue
            if _looks_like_private_key(abs_src, fname):
                private_keys.append(rel_dst)
                continue
            if at_root and fname in _RESERVED_CONTEXT_NAMES:
                reserved.append(rel_dst)
                continue
            swept.append((abs_src, rel_dst))
            try:
                size = os.path.getsize(abs_src)
            except OSError:
                size = 0
            total_bytes += size
            if size > largest[0]:
                largest = (size, rel_dst)

    if hidden:
        print(
            f"  Note: {len(hidden)} hidden path(s) not copied into the image: "
            f"{_sample(hidden)}. Move anything the agent opens at runtime out of "
            f"a dotted path."
        )
    if host_local:
        print(
            f"  Note: {len(host_local)} host-local path(s) not copied into the image: "
            f"{_sample(host_local)}. The image installs its own dependencies."
        )
    for rel_dst in sorted(private_keys):
        print(f"  Warning: not copying private key material into the image: {rel_dst}")
    for rel_dst in sorted(reserved):
        print(
            f"  Warning: the build context owns '{os.path.basename(rel_dst)}', so the "
            f"project's own copy is not included in the image"
        )
    if total_bytes > _LARGE_CONTEXT_BYTES:
        print(
            f"  Warning: sweeping {total_bytes // (1024 * 1024)} MB into this image, "
            f"largest is {largest[1]} at {largest[0] // (1024 * 1024)} MB. Everything "
            f"under the project root ships unless it is hidden or generated."
        )

    return swept


def _stub_destination(stub_file, stub_entrypoints):
    """Where to copy a stub so it overwrites the real file it replaces, falling back to flat if that's unsafe."""
    basename = os.path.basename(stub_file)
    entrypoint = stub_entrypoints.get(basename)
    if entrypoint:
        normalized = entrypoint.replace("\\", "/")
        if not normalized.startswith("/") and ".." not in normalized.split("/"):
            return normalized
        print(f"  Warning: unsafe entrypoint '{entrypoint}' for stub {basename}, placing flat instead")
    elif stub_entrypoints:
        print(f"  Warning: no entrypoint mapping for stub {basename}, placing flat instead")
    return basename


def _copy_files(output_dir, files_to_copy):
    """Copy each (src, dst) pair into output_dir, refusing to write outside it (e.g. via a symlinked destination parent)."""
    real_output_dir = os.path.realpath(output_dir)
    for src, dst in files_to_copy:
        if not os.path.isfile(src):
            print(f"  Warning: source file not found, skipping: {src}")
            continue
        dest_path = os.path.join(output_dir, dst)
        real_dest = os.path.realpath(dest_path)
        if os.path.commonpath([real_output_dir, real_dest]) != real_output_dir:
            print(f"  Warning: destination escapes build context, skipping: {dst}")
            continue
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(src, dest_path)


def generate_docker(
    yaml_path,
    agent_file,
    output_dir=None,
    grpc_stubs_dir=None,
    stub_files=None,
    project_dir=None,
    stub_entrypoints=None,
    requirements=None,
):
    """
    Generate a minimal Docker build context for an agent.

    Creates a directory containing a Dockerfile, requirements.txt, and all
    source files needed to run the agent with its own local controller.

    Args:
        yaml_path:         Path to the YAML agent definition.
        agent_file:        Path to the original Python agent implementation.
        output_dir:        Optional output directory (default: docker_container/<AgentName>/).
        grpc_stubs_dir:    Optional path to compiled gRPC stubs (default: <repo_root>/grpc_stubs).
        stub_files:        Optional list of agent stub files to copy into the context.
        project_dir:       Optional project root whose files are swept into the context.
        stub_entrypoints:  Optional {stub_basename: entrypoint} map for exact stub placement.
        requirements:   Optional list of extra pip packages this agent needs.
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

    # Sweep the whole project first; the explicit list below is copied on top.
    files_to_copy = []
    if project_dir:
        files_to_copy += _sweep_project_files(project_dir, exclude_dir=output_dir)

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
        (os.path.join(script_dir, "utils", "grpc_options.py"), "grpc_options.py"),
        (
            os.path.join(script_dir, "controller", "utils", "gpu_metrics.py"),
            "gpu_metrics.py",
        ),
        (os.path.join(script_dir, "llm", "bedrock.py"), "bedrock.py"),
    ]

    # Copy provided agent stubs, overwriting the swept real file at the same path
    if stub_files:
        for stub_file in stub_files:
            files_to_copy.append(
                (
                    os.path.abspath(stub_file),
                    _stub_destination(stub_file, stub_entrypoints or {}),
                )
            )

    files_to_copy.append((os.path.abspath(agent_file), os.path.basename(agent_file)))

    # Copy gRPC generated stubs if they exist
    if os.path.isdir(grpc_stubs_dir):
        for fname in os.listdir(grpc_stubs_dir):
            if fname.endswith(".py"):
                files_to_copy.append((os.path.join(grpc_stubs_dir, fname), fname))

    _copy_files(output_dir, files_to_copy)

    # Copy the YAML definition too
    shutil.copy2(
        os.path.abspath(yaml_path),
        os.path.join(output_dir, os.path.basename(yaml_path)),
    )

    # ---- Dockerfile ------------------------------------------------------
    agent_basename = os.path.basename(agent_file)
    dockerfile = f"""# syntax=docker/dockerfile:1
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -r requirements.txt

COPY . .

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
    project_dir=None,
    stub_entrypoints=None,
    requirements=None,
):
    """
    Generate a Docker build context for a workflow.

    Creates a directory containing a Dockerfile, requirements.txt,
    workflow_launcher.py, and all source files needed to run the workflow
    with its own local controller.

    Args:
        workflow_file:     Path to the workflow Python file.
        stub_files:        List of stub file paths to include.
        output_dir:        Optional output directory (default: docker_container/Workflow/).
        grpc_stubs_dir:    Optional path to compiled gRPC stubs (default: <repo_root>/grpc_stubs).
        project_dir:       Optional project root whose files are swept into the context.
        stub_entrypoints:  Optional {stub_basename: entrypoint} map for exact stub placement.
        requirements:   Optional list of extra pip packages this workflow needs.
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

    # Sweep the whole project first; the explicit list below is copied on top.
    files_to_copy = (
        _sweep_project_files(project_dir, exclude_dir=output_dir) if project_dir else []
    )

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
        (os.path.join(script_dir, "utils", "grpc_options.py"), "grpc_options.py"),
        *[
            (os.path.join(script_dir, "controller", "utils", name), name)
            for name in ("gpu_metrics.py", "session_logging.py")
        ],
    ]
          
    # Copy stub files, overwriting the swept real file at the same path
    for stub_file in stub_files:
        files_to_copy.append(
            (
                os.path.abspath(stub_file),
                _stub_destination(stub_file, stub_entrypoints or {}),
            )
        )

    # Copy gRPC generated stubs if they exist
    if os.path.isdir(grpc_stubs_dir):
        for fname in os.listdir(grpc_stubs_dir):
            if fname.endswith(".py"):
                files_to_copy.append((os.path.join(grpc_stubs_dir, fname), fname))

    _copy_files(output_dir, files_to_copy)

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
    dockerfile = f"""# syntax=docker/dockerfile:1
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -r requirements.txt

COPY . .

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
