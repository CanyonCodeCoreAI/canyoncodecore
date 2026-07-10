"""
Ventis CLI

Entry point for the `ventis` command. Provides three subcommands:
    ventis new-project <name>   — Scaffold a new Ventis project
    ventis build                — Generate stubs and build Docker images
    ventis deploy               — Launch agents via the Global Controller
"""

import argparse
import glob
import logging
import os
import shutil
import subprocess
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ventis")
DEFAULT_DOCKER_PLATFORM = "linux/amd64"
DEFAULT_CONFIG_PATH = "config/global_controller.yaml"
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


def _docker_platform():
    """Return the target Docker platform for portable runtime images."""
    return os.environ.get("VENTIS_DOCKER_PLATFORM", DEFAULT_DOCKER_PLATFORM)


def _docker_build_cmd(*args):
    """Build a Docker build command with an explicit target platform."""
    return ["docker", "build", "--platform", _docker_platform(), *args]


def _docker_available():
    if not shutil.which("docker"):
        return False

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    return result.returncode == 0


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


def _preflight_ec2_deploy(config, project_dir):
    ec2_cfg = config.get("ec2", {})
    missing = [key for key in EC2_REQUIRED_CONFIG_KEYS if not ec2_cfg.get(key)]
    if missing:
        raise RuntimeError(
            f"EC2 deploy preflight failed: missing ec2 config keys: {', '.join(sorted(missing))}"
        )

    _require_docker_for_ec2("deploy")
    _ensure_grpc_stubs_importable(project_dir)


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

    # Copy the entire templates tree into the new project
    shutil.copytree(templates_dir, project_dir)

    # Create empty output directories
    os.makedirs(os.path.join(project_dir, "stubs"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "grpc_stubs"), exist_ok=True)

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

    Must be run from the project root (where config/ lives).
    """
    config_path = args.config
    if not os.path.isfile(config_path):
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = _load_config(config_path)
    agents = config.get("agents", [])
    project_dir = os.getcwd()
    package_dir = _get_package_dir()

    # -------------------------------------------------------------- #
    #  Step 1: Discover agent YAML files and generate Python stubs    #
    # -------------------------------------------------------------- #
    agents_dir = os.path.join(project_dir, "agents")
    stubs_dir = os.path.join(project_dir, "stubs")
    os.makedirs(stubs_dir, exist_ok=True)

    from ventis.stub_generator import (
        generate_stub,
        generate_docker,
        generate_workflow_docker,
    )

    yaml_files = glob.glob(os.path.join(agents_dir, "*.yaml"))
    if not yaml_files:
        logger.warning("No agent YAML files found in %s", agents_dir)

    stub_paths = []
    for yaml_path in yaml_files:
        base_name = os.path.splitext(os.path.basename(yaml_path))[0]
        output_path = os.path.join(stubs_dir, f"{base_name}_stub.py")
        logger.info("Generating stub: %s -> %s", yaml_path, output_path)
        generate_stub(yaml_path, output_path)
        stub_paths.append(output_path)

    # -------------------------------------------------------------- #
    #  Step 2: Compile gRPC protobuf stubs                            #
    # -------------------------------------------------------------- #
    grpc_stubs_dir = os.path.join(project_dir, "grpc_stubs")
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
    #  Step 4: Generate Docker contexts and build images               #
    # -------------------------------------------------------------- #
    for agent_cfg in agents:
        agent_name = agent_cfg["name"]
        agent_type = agent_cfg.get("type", "agent")

        if agent_type == "workflow":
            # Workflow container
            workflow_file = agent_cfg.get("workflow_file")
            if not workflow_file:
                logger.warning(
                    "Skipping workflow '%s': no workflow_file specified", agent_name
                )
                continue

            workflow_path = os.path.join(project_dir, workflow_file)
            if not os.path.isfile(workflow_path):
                logger.error("Workflow file not found: %s", workflow_path)
                continue

            docker_context = os.path.join(project_dir, "docker_container", "Workflow")
            logger.info("Generating workflow Docker context for '%s'", agent_name)
            generate_workflow_docker(
                workflow_path,
                stub_paths,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
            )

            image_name = f"ventis-{agent_name.lower()}"
            logger.info("Building Docker image: %s", image_name)
            subprocess.run(
                _docker_build_cmd("-t", image_name, docker_context), check=True
            )

        else:
            # Agent container
            entrypoint = agent_cfg.get("entrypoint")
            if not entrypoint:
                logger.warning(
                    "Skipping agent '%s': no entrypoint specified", agent_name
                )
                continue

            agent_file = os.path.join(project_dir, entrypoint)
            if not os.path.isfile(agent_file):
                logger.error("Agent file not found: %s", agent_file)
                continue

            # Find matching YAML by agent name
            matching_yaml = None
            for yaml_path in yaml_files:
                import yaml

                with open(yaml_path) as f:
                    ydata = yaml.safe_load(f)
                if ydata.get("agent", {}).get("name") == agent_name:
                    matching_yaml = yaml_path
                    break

            if not matching_yaml:
                logger.warning(
                    "No YAML definition found for agent '%s', skipping Docker",
                    agent_name,
                )
                continue

            docker_context = os.path.join(project_dir, "docker_container", agent_name)
            logger.info("Generating Docker context for '%s'", agent_name)
            generate_docker(
                matching_yaml,
                agent_file,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                stub_files=stub_paths,
            )

            image_name = f"ventis-{agent_name.lower()}"
            logger.info("Building Docker image: %s", image_name)
            subprocess.run(
                _docker_build_cmd("-t", image_name, docker_context), check=True
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
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = _load_config(config_path)
    project_dir = os.getcwd()

    _ensure_grpc_stubs_importable(project_dir)

    if any(
        agent.get("provider", "local").upper() == "EC2"
        for agent in config.get("agents", [])
    ):
        _preflight_ec2_deploy(config, project_dir)

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
    project_dir = os.getcwd()

    paths_to_clean = [
        os.path.join(project_dir, "stubs"),
        os.path.join(project_dir, "grpc_stubs"),
        os.path.join(project_dir, "docker_container"),
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
