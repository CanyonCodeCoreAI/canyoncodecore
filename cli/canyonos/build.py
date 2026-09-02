"""
Generate stubs, compile gRPC protos, generate Docker contexts,
and build Docker images.

Must be run from the project root (where config/ lives).
"""

import glob
import json
import os
import shutil
import subprocess
import sys

DEFAULT_DOCKER_PLATFORM = "linux/amd64"
DEFAULT_CONFIG_PATH = "config/global_controller.yaml"


def _get_package_dir():
    """Return the absolute path to the installed ventis package directory."""
    import ventis

    return os.path.dirname(os.path.abspath(ventis.__file__))


def _load_config(config_path):
    """Load a YAML config file."""
    import yaml

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def _normalize_requirements(agent_cfg):
    """Return an agent's `requirements` list, or [] if absent/null/malformed."""
    requirements = agent_cfg.get("requirements") or []
    if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
        # The requirements list is bad, assuming file has no requirements and logging error
        print(
            f"Agent '{agent_cfg.get('name')}': `requirements` must be a list of "
            f"strings, got {requirements!r}; ignoring."
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


def run_build(config_path=DEFAULT_CONFIG_PATH):
    if not os.path.isfile(config_path):
        print(f"Config file not found: {config_path}")
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
        print(f"No agent YAML files found in {agents_dir}")

    import yaml

    # Looks up a config entry's YAML and to map stubs to entrypoints.
    yaml_by_name = {}
    for yaml_path in yaml_files:
        with open(yaml_path) as f:
            name = yaml.safe_load(f).get("agent", {}).get("name")
        if name:
            yaml_by_name[name] = yaml_path

    # Maps each generated stub's basename to its agent's entrypoint path, so a
    # stub can also be placed at its nested, entrypoint-mirrored location.
    entrypoints_by_name = {a["name"]: a.get("entrypoint") for a in agents}
    stub_entrypoints = {
        f"{os.path.splitext(os.path.basename(p))[0]}.py": entrypoints_by_name[n]
        for n, p in yaml_by_name.items()
        if entrypoints_by_name.get(n)
    }

    stub_paths = []
    for yaml_path in yaml_files:
        base_name = os.path.splitext(os.path.basename(yaml_path))[0]
        output_path = os.path.join(stubs_dir, f"{base_name}.py")
        print(f"Generating stub: {yaml_path} -> {output_path}")
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
        print(f"Compiling gRPC proto: {proto_file}")
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
                print(f"Skipping workflow '{agent_name}': no workflow_file specified")
                continue

            workflow_path = os.path.join(project_dir, workflow_file)
            if not os.path.isfile(workflow_path):
                print(f"Workflow file not found: {workflow_path}")
                continue

            docker_context = os.path.join(project_dir, "docker_container", "Workflow")
            print(f"Generating workflow Docker context for '{agent_name}'")
            generate_workflow_docker(
                workflow_path,
                stub_paths,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                api_port=agent_cfg.get("api_port", 8080),
                project_dir=project_dir,
                requirements=_normalize_requirements(agent_cfg),
                # Stubs are placed both flat and at their entrypoint-mirrored path,
                # so both flat and nested import styles resolve to the stub.
                stub_entrypoints=stub_entrypoints,
            )

        else:
            # Agent container
            entrypoint = agent_cfg.get("entrypoint")
            if not entrypoint:
                print(f"Skipping agent '{agent_name}': no entrypoint specified")
                continue

            agent_file = os.path.join(project_dir, entrypoint)
            if not os.path.isfile(agent_file):
                print(f"Agent file not found: {agent_file}")
                continue

            # Find matching YAML by agent name
            matching_yaml = yaml_by_name.get(agent_name)
            if not matching_yaml:
                print(f"No YAML definition found for agent '{agent_name}', skipping Docker")
                continue

            docker_context = os.path.join(project_dir, "docker_container", agent_name)
            print(f"Generating Docker context for '{agent_name}'")
            generate_docker(
                matching_yaml,
                agent_file,
                output_dir=docker_context,
                grpc_stubs_dir=grpc_stubs_dir,
                stub_files=stub_paths,
                project_dir=project_dir,
                requirements=_normalize_requirements(agent_cfg),
                # Same reasoning as the workflow call above: stubs are placed both
                # flat and at their entrypoint-mirrored path.
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
        print("No Docker images to build.")
    elif _docker_available() and _docker_available(("docker", "buildx", "version")):
        docker_container_dir = os.path.join(project_dir, "docker_container")
        os.makedirs(docker_container_dir, exist_ok=True)
        bake_file_path = os.path.join(docker_container_dir, "docker-bake.json")
        _write_bake_file(bake_targets, bake_file_path, _docker_platform())

        target_names = [target["name"] for target in bake_targets]
        print(f"Building {len(bake_targets)} Docker image(s) via `docker buildx bake`.")
        subprocess.run(
            ["docker", "buildx", "bake", "--file", bake_file_path, *target_names],
            check=True,
        )
    else:
        print("docker buildx unavailable; falling back to sequential `docker build`.")
        for target in bake_targets:
            print(f"Building Docker image: {target['image_name']}")
            subprocess.run(
                _docker_build_cmd("-t", target["image_name"], target["context"]),
                check=True,
            )

    print("Build complete.")
