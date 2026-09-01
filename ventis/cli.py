"""
Ventis CLI

Entry point for the `ventis` command. Provides three subcommands:
    ventis new-project <name>   — Scaffold a new Ventis project
    ventis build                — Generate stubs and build Docker images
    ventis deploy               — Launch agents via the Global Controller
"""

import argparse
import glob
import json
import logging
import os
import shutil
import subprocess
import sys

from ventis.controller.utils.env_file import resolve_env_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ventis")
DEFAULT_DOCKER_PLATFORM = "linux/amd64"
ARTIFACT_DIR_NAME = ".car"
SOURCE_DIR_NAME = "app"
DEFAULT_CONFIG_PATH = f"{ARTIFACT_DIR_NAME}/config/global_controller.yaml"

EC2_REQUIRED_CONFIG_KEYS = (
    "ami_id",
    "subnet_id",
    "security_group_ids",
    "region",
)


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def _get_templates_dir():
    """Return the absolute path to the bundled templates directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _get_package_dir():
    """Return the absolute path to the ventis package directory."""
    return os.path.dirname(os.path.abspath(__file__))


def _load_config(config_path):
    """Load a YAML config file."""
    import yaml

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _report_missing_config(config_path):
    """Report a missing config, and say so when the command ran inside `.car`.

    Commands read `.car` relative to the directory they run from, so running
    one inside `.car` looks for `.car/.car/...`. Name that rather than
    reporting an absent path the caller never typed.
    """
    logger.error("Config file not found: %s", config_path)
    if os.path.isabs(config_path):
        return
    if os.path.basename(os.getcwd()) == ARTIFACT_DIR_NAME and os.path.isfile(
        os.path.relpath(config_path, ARTIFACT_DIR_NAME)
    ):
        logger.error(
            "This is the inside of %s. Canyon commands run from the "
            "application root, one level up: `cd ..` first.",
            ARTIFACT_DIR_NAME,
        )


def _artifact_root():
    """Return `.car` below the application root the command runs from."""
    return os.path.join(os.getcwd(), ARTIFACT_DIR_NAME)


def _source_root(artifact_root):
    """Return the duplicated application source inside an artifact root."""
    return os.path.join(artifact_root, SOURCE_DIR_NAME)


def _container_path(rel_path, description):
    """Return `rel_path` as a container-relative POSIX path, or None if it escapes /app."""
    normalized = str(rel_path).replace("\\", "/")
    if os.path.isabs(normalized) or ".." in normalized.split("/"):
        logger.error("%s must stay inside the source copy: %s", description, rel_path)
        return None
    return normalized


def _normalize_requirements(agent_cfg):
    """Return an agent's `requirements` list, or [] if absent/null/malformed."""
    requirements = agent_cfg.get("requirements") or []
    if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
        # The requirements list is bad, assuming file has no requirements and logging error
        logger.warning(
            "Agent '%s': `requirements` must be a list of strings, got %r; ignoring.",
            agent_cfg.get("name"),
            requirements,
        )
        return []
    return requirements


def _docker_platform():
    """Return the target Docker platform for portable runtime images."""
    return os.environ.get("VENTIS_DOCKER_PLATFORM", DEFAULT_DOCKER_PLATFORM)


def _docker_build_cmd(*args):
    """Build a Docker build command with an explicit target platform."""
    return ["docker", "build", "--platform", _docker_platform(), *args]


def _docker_available(probe_cmd=("docker", "info")):
    if not shutil.which("docker"):
        return False

    try:
        result = subprocess.run(
            list(probe_cmd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    return result.returncode == 0


def _write_bake_file(bake_targets, bake_file_path, platform):
    """Write a docker-buildx-bake JSON file describing all build targets.

    Context paths are written absolute: `docker buildx bake` resolves relative
    `context` values against the invocation cwd (not the bake file's own
    directory), so an absolute path sidesteps that ambiguity entirely.
    """
    bake_config = {
        "target": {
            target["name"]: {
                "context": os.path.abspath(target["context"]),
                "dockerfile": "Dockerfile",
                "tags": [target["image_name"]],
                "platforms": [platform],
                "output": ["type=docker"],
              # type=docker could be changed to tarring it up, which would be
              # faster but skipped because that change would alter ventis deploy
            }
            for target in bake_targets
        }
    }
    with open(bake_file_path, "w") as f:
        json.dump(bake_config, f, indent=2)
    return bake_file_path


def _require_docker_for_ec2(command_name):
    if _docker_available():
        return
    raise RuntimeError(
        f"EC2-backed `ventis {command_name}` requires local Docker, but Docker is unavailable "
        "or unreachable."
    )


def _ensure_grpc_stubs_importable(project_dir):
    grpc_stubs_dir = os.path.join(project_dir, "grpc_stubs")
    if grpc_stubs_dir not in sys.path:
        sys.path.insert(0, grpc_stubs_dir)

    try:
        __import__("local_controler_pb2")
        __import__("local_controler_pb2_grpc")
    except ImportError as exc:
        raise RuntimeError(
            "Deploy failed: generated grpc_stubs are missing or not importable. "
            "Run `ventis build` on this host first."
        ) from exc


def _preflight_ec2_deploy(config):
    ec2_cfg = config.get("ec2", {})
    missing = [key for key in EC2_REQUIRED_CONFIG_KEYS if not ec2_cfg.get(key)]
    if missing:
        raise RuntimeError(
            f"EC2 deploy preflight failed: missing ec2 config keys: {', '.join(sorted(missing))}"
        )

    _require_docker_for_ec2("deploy")


# ------------------------------------------------------------------ #
#  ventis new-project                                                  #
# ------------------------------------------------------------------ #


def cmd_new_project(args):
    """Scaffold a new Ventis project."""
    project_name = args.name
    project_dir = os.path.abspath(project_name)

    if os.path.exists(project_dir):
        logger.error("Directory '%s' already exists.", project_name)
        sys.exit(1)

    templates_dir = _get_templates_dir()
    if not os.path.isdir(templates_dir):
        logger.error("Templates directory not found at %s", templates_dir)
        sys.exit(1)

    # Canyon-owned files live under .car: config/ next to the application
    # source copy that becomes /app. Only the README belongs to the developer.
    artifact_root = os.path.join(project_dir, ARTIFACT_DIR_NAME)
    source_root = _source_root(artifact_root)
    shutil.copytree(templates_dir, source_root)

    template_config = os.path.join(source_root, "config")
    if os.path.isdir(template_config):
        shutil.move(template_config, os.path.join(artifact_root, "config"))
    template_readme = os.path.join(source_root, "README.md")
    if os.path.isfile(template_readme):
        shutil.move(template_readme, os.path.join(project_dir, "README.md"))

    # Create empty output directories
    os.makedirs(os.path.join(artifact_root, "stubs"), exist_ok=True)
    os.makedirs(os.path.join(artifact_root, "grpc_stubs"), exist_ok=True)

    logger.info("Created new Ventis project: %s", project_dir)
    logger.info("")
    logger.info("  cd %s", project_name)
    logger.info("  ventis build")
    logger.info("  ventis deploy")


# ------------------------------------------------------------------ #
#  ventis build                                                        #
# ------------------------------------------------------------------ #


def cmd_build(args):
    """
    Generate stubs, compile gRPC protos, generate Docker contexts,
    and build Docker images.

    Must be run from the application root, the directory holding `.car`.
    """
    config_path = args.config
    if not os.path.isfile(config_path):
        _report_missing_config(config_path)
        sys.exit(1)

    config = _load_config(config_path)
    agents = config.get("agents", [])
    artifact_root = _artifact_root()
    source_root = _source_root(artifact_root)
    config_dir = os.path.dirname(os.path.abspath(config_path))
    package_dir = _get_package_dir()

    if not os.path.isdir(source_root):
        logger.error(
            "Application source copy not found: %s. Duplicate the application "
            "source there before building.",
            source_root,
        )
        sys.exit(1)

    # -------------------------------------------------------------- #
    #  Step 1: Discover agent declarations and generate Python stubs  #
    # -------------------------------------------------------------- #
    stubs_dir = os.path.join(artifact_root, "stubs")
    os.makedirs(stubs_dir, exist_ok=True)

    from ventis.stub_generator import (
        generate_stub,
        generate_docker,
        generate_workflow_docker,
    )

    import yaml

    # Agent declarations sit in config/ beside the manifest. The manifest and
    # policy.yaml declare no `agent.name`, so they fall out here.
    yaml_by_name = {}
    for yaml_path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
        with open(yaml_path) as f:
            doc = yaml.safe_load(f)
        agent = doc.get("agent") if isinstance(doc, dict) else None
        name = agent.get("name") if isinstance(agent, dict) else None
        if name:
            yaml_by_name[name] = yaml_path
    if not yaml_by_name:
        logger.warning("No agent YAML declarations found in %s", config_dir)

    entrypoints_by_name = {a["name"]: a.get("entrypoint") for a in agents}

    # A stub replaces the real module at its own entrypoint path, so callers
    # keep importing the agent exactly where the source put it. Placing it
    # anywhere else would leave that import resolving to the real class and
    # running the agent in-process instead of over gRPC.
    stub_specs = []
    for name, yaml_path in yaml_by_name.items():
        entrypoint = entrypoints_by_name.get(name)
        if not entrypoint:
            logger.warning(
                "Agent '%s' is declared in %s but has no config entry; skipping its stub",
                name,
                yaml_path,
            )
            continue
        dest = _container_path(entrypoint, f"entrypoint for agent '{name}'")
        if dest is None:
            continue
        output_path = os.path.join(stubs_dir, dest)
        logger.info("Generating stub: %s -> %s", yaml_path, output_path)
        generate_stub(yaml_path, output_path)
        stub_specs.append((name, output_path, dest))

    # -------------------------------------------------------------- #
    #  Step 2: Compile gRPC protobuf stubs                            #
    # -------------------------------------------------------------- #
    grpc_stubs_dir = os.path.join(artifact_root, "grpc_stubs")
    os.makedirs(grpc_stubs_dir, exist_ok=True)

    proto_dir = os.path.join(package_dir, "controller", "proto")
    proto_files = glob.glob(os.path.join(proto_dir, "*.proto"))

    for proto_file in proto_files:
        logger.info("Compiling gRPC proto: %s", proto_file)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "grpc_tools.protoc",
                f"-I{proto_dir}",
                f"--python_out={grpc_stubs_dir}",
                f"--grpc_python_out={grpc_stubs_dir}",
                proto_file,
            ],
            check=True,
        )

    # -------------------------------------------------------------- #
    #  Step 3: Generate Docker contexts                               #
    # -------------------------------------------------------------- #
    bake_targets = []
    unbuildable = []
    for agent_cfg in agents:
        agent_name = agent_cfg["name"]
        agent_type = agent_cfg.get("type", "agent")

        if agent_type == "workflow":
            # Workflow container
            workflow_file = agent_cfg.get("workflow_file")
            if not workflow_file:
                logger.error("Workflow '%s': no workflow_file specified", agent_name)
                unbuildable.append(agent_name)
                continue

            workflow_rel = _container_path(
                workflow_file, f"workflow_file for '{agent_name}'"
            )
            if workflow_rel is None:
                unbuildable.append(agent_name)
                continue

            workflow_path = os.path.join(source_root, workflow_rel)
            if not os.path.isfile(workflow_path):
                logger.error("Workflow file not found: %s", workflow_path)
                unbuildable.append(agent_name)
                continue

            docker_context = os.path.join(artifact_root, "docker_container", "Workflow")
            logger.info("Generating workflow Docker context for '%s'", agent_name)
            generate_workflow_docker(
                workflow_path,
                [(path, dest) for _, path, dest in stub_specs],
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                api_port=agent_cfg.get("api_port", 8080),
                project_dir=source_root,
                workflow_entrypoint=workflow_rel,
                requirements=_normalize_requirements(agent_cfg),
            )

        else:
            # Agent container
            entrypoint = agent_cfg.get("entrypoint")
            if not entrypoint:
                logger.error("Agent '%s': no entrypoint specified", agent_name)
                unbuildable.append(agent_name)
                continue

            entrypoint_rel = _container_path(
                entrypoint, f"entrypoint for agent '{agent_name}'"
            )
            if entrypoint_rel is None:
                unbuildable.append(agent_name)
                continue

            agent_file = os.path.join(source_root, entrypoint_rel)
            if not os.path.isfile(agent_file):
                logger.error("Agent file not found: %s", agent_file)
                unbuildable.append(agent_name)
                continue

            # Find matching YAML by agent name
            matching_yaml = yaml_by_name.get(agent_name)
            if not matching_yaml:
                logger.error(
                    "No declaration for agent '%s': %s holds no *.yaml with "
                    "`agent.name: %s`",
                    agent_name,
                    config_dir,
                    agent_name,
                )
                unbuildable.append(agent_name)
                continue

            docker_context = os.path.join(artifact_root, "docker_container", agent_name)
            logger.info("Generating Docker context for '%s'", agent_name)
            # An agent gets every stub but its own: the one at its entrypoint
            # would replace the implementation it is supposed to run.
            generate_docker(
                matching_yaml,
                agent_file,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                stub_files=[
                    (path, dest)
                    for name, path, dest in stub_specs
                    if name != agent_name
                ],
                project_dir=source_root,
                agent_entrypoint=entrypoint_rel,
                requirements=_normalize_requirements(agent_cfg),
            )

        bake_targets.append(
            {
                "name": agent_name.lower(),
                "context": docker_context,
                "image_name": f"ventis-{agent_name.lower()}",
            }
        )

    # Stop before building a partial set. Deploy would otherwise fail on the
    # missing image, naming a container instead of the entry behind it.
    if unbuildable:
        logger.error(
            "Build aborted: no image can be produced for %s",
            ", ".join(unbuildable),
        )
        sys.exit(1)

    # -------------------------------------------------------------- #
    #  Step 4: Build all Docker images                                #
    # -------------------------------------------------------------- #
    if not bake_targets:
        logger.info("No Docker images to build.")
    elif _docker_available() and _docker_available(("docker", "buildx", "version")):
        docker_container_dir = os.path.join(artifact_root, "docker_container")
        os.makedirs(docker_container_dir, exist_ok=True)
        bake_file_path = os.path.join(docker_container_dir, "docker-bake.json")
        _write_bake_file(bake_targets, bake_file_path, _docker_platform())

        target_names = [target["name"] for target in bake_targets]
        logger.info(
            "Building %d Docker image(s) via `docker buildx bake`.",
            len(bake_targets),
        )
        subprocess.run(
            ["docker", "buildx", "bake", "--file", bake_file_path, *target_names],
            check=True,
        )
    else:
        logger.info(
            "docker buildx unavailable; falling back to sequential `docker build`."
        )
        for target in bake_targets:
            logger.info("Building Docker image: %s", target["image_name"])
            subprocess.run(
                _docker_build_cmd("-t", target["image_name"], target["context"]),
                check=True,
            )

    logger.info("Build complete.")


# ------------------------------------------------------------------ #
#  ventis deploy                                                       #
# ------------------------------------------------------------------ #


def cmd_deploy(args):
    """
    Launch the Global Controller, which starts Redis containers,
    agent containers, and enters the health-monitoring loop.
    """
    import signal
    import atexit

    config_path = args.config
    if not os.path.isfile(config_path):
        _report_missing_config(config_path)
        sys.exit(1)

    config = _load_config(config_path)
    artifact_root = _artifact_root()

    # Fail here rather than after a fleet of containers is already up without
    # the API keys they need.
    try:
        resolve_env_file(config)
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    _ensure_grpc_stubs_importable(artifact_root)

    if any(
        agent.get("provider", "local").upper() == "EC2"
        for agent in config.get("agents", [])
    ):
        _preflight_ec2_deploy(config)

    from ventis.controller.global_controller import GlobalController

    controller = GlobalController(config_path)

    # Graceful shutdown on Ctrl+C / SIGTERM
    def _signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        controller.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(controller.cleanup)

    # SIGHUP reloads config in place without tearing down any agent.
    def _reload_handler(sig, frame):
        logger.info("Received SIGHUP, reloading config...")
        try:
            controller.reload_config()
        except Exception as e:
            logger.error("Reload failed: %s", e)

    signal.signal(signal.SIGHUP, _reload_handler)

    logger.info("Deploying from config: %s", config_path)
    controller.launch_docker_agents()
    controller._wait_for_healthy()
    controller.run()


# ------------------------------------------------------------------ #
#  ventis clean                                                        #
# ------------------------------------------------------------------ #


def cmd_clean(args):
    """
    Remove generated stubs, gRPC files, and Docker build contexts.
    """
    artifact_root = _artifact_root()

    paths_to_clean = [
        os.path.join(artifact_root, "stubs"),
        os.path.join(artifact_root, "grpc_stubs"),
        os.path.join(artifact_root, "docker_container"),
    ]

    for path in paths_to_clean:
        if os.path.exists(path):
            logger.info("Cleaning %s...", path)
            if os.path.isdir(path):
                import shutil

                shutil.rmtree(path)
            else:
                os.remove(path)

    logger.info("Clean complete.")


# ------------------------------------------------------------------ #
#  Main entry point                                                    #
# ------------------------------------------------------------------ #


def main():
    parser = argparse.ArgumentParser(
        prog="ventis",
        description="Ventis — Distributed Agent Orchestration Framework",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ventis new-project <name>
    new_proj = subparsers.add_parser(
        "new-project",
        help="Scaffold a new Ventis project",
    )
    new_proj.add_argument("name", help="Name of the project directory to create")
    new_proj.set_defaults(func=cmd_new_project)

    # ventis build
    build = subparsers.add_parser(
        "build",
        help="Generate stubs, compile protos, and build Docker images",
    )
    build.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to global controller config (default: {DEFAULT_CONFIG_PATH})",
    )
    build.set_defaults(func=cmd_build)

    # ventis deploy
    deploy = subparsers.add_parser(
        "deploy",
        help="Launch agents via the Global Controller",
    )
    deploy.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to global controller config (default: {DEFAULT_CONFIG_PATH})",
    )
    deploy.set_defaults(func=cmd_deploy)

    # ventis clean
    clean = subparsers.add_parser(
        "clean",
        help="Remove generated stubs, compiled protos, and Docker contexts",
    )
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
