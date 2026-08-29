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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ventis")
DEFAULT_DOCKER_PLATFORM = "linux/amd64"
DEFAULT_CONFIG_PATH = ".car/config/global_controller.yaml"
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


def _artifact_root(config_path):
    """Return ``<artifact root>`` for ``<artifact root>/config/<config>``."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    return os.path.dirname(config_dir)


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

    # Canyon-owned files live under .car; keep the project README at the root.
    artifact_root = os.path.join(project_dir, ".car")
    shutil.copytree(templates_dir, artifact_root)
    template_readme = os.path.join(artifact_root, "README.md")
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

    Must be run from the source project root (where .car/ lives).
    """
    config_path = args.config
    if not os.path.isfile(config_path):
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = _load_config(config_path)
    agents = config.get("agents", [])
    project_dir = os.getcwd()
    artifact_root = _artifact_root(config_path)
    package_dir = _get_package_dir()

    # -------------------------------------------------------------- #
    #  Step 1: Discover agent YAML files and generate Python stubs    #
    # -------------------------------------------------------------- #
    agents_dir = os.path.join(artifact_root, "agents")
    stubs_dir = os.path.join(artifact_root, "stubs")
    os.makedirs(stubs_dir, exist_ok=True)

    from ventis.stub_generator import (
        generate_stub,
        generate_docker,
        generate_workflow_docker,
    )

    yaml_files = glob.glob(os.path.join(agents_dir, "*.yaml"))
    if not yaml_files:
        logger.warning("No agent YAML files found in %s", agents_dir)

    import yaml

    yaml_by_name = {}
    for yaml_path in yaml_files:
        with open(yaml_path) as f:
            name = yaml.safe_load(f).get("agent", {}).get("name")
        if name:
            yaml_by_name[name] = yaml_path

    entrypoints_by_name = {a["name"]: a.get("entrypoint") for a in agents}
    stub_entrypoints = {
        f"{os.path.splitext(os.path.basename(path))[0]}.py": entrypoints_by_name[name]
        for name, path in yaml_by_name.items()
        if entrypoints_by_name.get(name)
    }

    stub_paths = []
    for yaml_path in yaml_files:
        base_name = os.path.splitext(os.path.basename(yaml_path))[0]
        output_path = os.path.join(stubs_dir, f"{base_name}.py")
        logger.info("Generating stub: %s -> %s", yaml_path, output_path)
        generate_stub(yaml_path, output_path)
        stub_paths.append(output_path)

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
    #  Step 4: Generate Docker contexts                               #
    # -------------------------------------------------------------- #
    bake_targets = []
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

            workflow_path = os.path.join(artifact_root, workflow_file)
            if not os.path.isfile(workflow_path):
                logger.error("Workflow file not found: %s", workflow_path)
                continue

            docker_context = os.path.join(artifact_root, "docker_container", "Workflow")
            logger.info("Generating workflow Docker context for '%s'", agent_name)
            generate_workflow_docker(
                workflow_path,
                stub_paths,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                api_port=agent_cfg.get("api_port", 8080),
                requirements=_normalize_requirements(agent_cfg),
                project_dir=project_dir,
                stub_entrypoints=stub_entrypoints,
            )

        else:
            # Agent container
            entrypoint = agent_cfg.get("entrypoint")
            if not entrypoint:
                logger.warning(
                    "Skipping agent '%s': no entrypoint specified", agent_name
                )
                continue

            agent_file = os.path.join(artifact_root, entrypoint)
            if not os.path.isfile(agent_file):
                logger.error("Agent file not found: %s", agent_file)
                continue

            # Find matching YAML by agent name
            matching_yaml = yaml_by_name.get(agent_name)

            if not matching_yaml:
                logger.warning(
                    "No YAML definition found for agent '%s', skipping Docker",
                    agent_name,
                )
                continue

            docker_context = os.path.join(artifact_root, "docker_container", agent_name)
            logger.info("Generating Docker context for '%s'", agent_name)
            generate_docker(
                matching_yaml,
                agent_file,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                stub_files=stub_paths,
                requirements=_normalize_requirements(agent_cfg),
                project_dir=project_dir,
                stub_entrypoints=stub_entrypoints,
            )

        bake_targets.append(
            {
                "name": agent_name.lower(),
                "context": docker_context,
                "image_name": f"ventis-{agent_name.lower()}",
            }
        )

    # -------------------------------------------------------------- #
    #  Step 5: Build all Docker images                                #
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
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    config = _load_config(config_path)
    artifact_root = _artifact_root(config_path)

    _ensure_grpc_stubs_importable(artifact_root)

    if any(
        agent.get("provider", "local").upper() == "EC2"
        for agent in config.get("agents", [])
    ):
        _preflight_ec2_deploy(config, artifact_root)

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
    config_path = getattr(args, "config", DEFAULT_CONFIG_PATH)
    artifact_root = _artifact_root(config_path)
    generated_names = ("stubs", "grpc_stubs", "docker_container")
    paths_to_clean = [os.path.join(artifact_root, name) for name in generated_names]

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
    clean.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Select the artifact layout via its config (default: {DEFAULT_CONFIG_PATH})",
    )
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
