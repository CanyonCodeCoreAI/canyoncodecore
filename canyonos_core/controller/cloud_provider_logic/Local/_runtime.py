"""
Local runtime helpers for CanyonOS.

This module is the local-provider backend for `provider: local` agents.
It keeps the existing Docker launch/teardown behavior while letting
InstanceManager stay focused on orchestration and persistence.
"""

import logging
import os

from canyonos_core.controller.utils.env_file import env_file_args

logger = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
CONTAINER_PORT = 50051
PROVIDER = "local"
MAX_PORT_ATTEMPTS = 50
NETWORK = "canyonos-local"
_controller = None


def _require_controller():
    if _controller is None:
        raise RuntimeError("Local runtime controller is not configured.")
    return _controller


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def validate_config():
    return None


def provision_instance(spec, replica_index, next_host_port):
    host = spec.get("host", DEFAULT_HOST)
    host_port = int(spec.get("host_port", spec.get("port", next_host_port(host))))
    agent_name = spec["name"]

    return {
        "provider": PROVIDER,
        "host": host,
        "host_port": host_port,
        "redis_host": f"canyonos-redis-{host.replace('.', '-')}",
        "runtime_id": f"canyonos-{PROVIDER}-{agent_name.lower()}-{replica_index}",
        "user": spec.get("user"),
    }


def bootstrap_instance(provisioned, spec, replica_index, agent_id):
    agent_name = spec["name"]
    resources = spec.get("resources", {})
    ctrl_type = spec.get("type", "agent")
    image = f"canyonos-{agent_name.lower()}"
    host = provisioned["host"]
    host_port = provisioned["host_port"]
    user = provisioned.get("user")
    redis_host = provisioned["redis_host"]
    runtime_id = provisioned["runtime_id"]

    inspect = _require_controller()._run_cmd(
        ["docker", "inspect", "-f", "{{.State.Running}}", runtime_id], host, user
    )
    if inspect.returncode == 0 and inspect.stdout.strip() == "true":
        logger.warning(
            "No Redis record for %s but a container with that name is already running; "
            "treating it as orphaned and recreating.",
            runtime_id,
        )
        _require_controller()._run_cmd(["docker", "rm", "-f", runtime_id], host, user)

    for attempt in range(MAX_PORT_ATTEMPTS):
        cmd = [
            "docker",
            "run",
            "-d",
            "-it",
            "--network",
            NETWORK,
            "--name",
            runtime_id,
            "-p",
            f"{host_port}:{CONTAINER_PORT}",
            "-e",
            f"CANYONOS_AGENT_PORT={CONTAINER_PORT}",
            "-e",
            f"CANYONOS_AGENT_HOST={runtime_id}",
            "-e",
            f"CANYONOS_REDIS_HOST={redis_host}",
            "-e",
            f"CANYONOS_REDIS_PORT={spec.get('redis_port', 6379)}",
            "-e",
            f"CANYONOS_POLL_INTERVAL={_require_controller().config.get('poll_interval', 5)}",
            # Route the agent's boto3 Bedrock calls through the in-container LLM
            # proxy (started by LocalController) so token/cost telemetry is captured.
            "-e",
            "AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://127.0.0.1:8081/bedrock",
        ]

        # LLM stub is a `canyonos test`-only control. `canyonos test` injects
        # CANYONOS_LLM_STUB_TEXT into THIS controller's (GC container) env; a
        # normal `canyonos deploy` never does (run_container only sets it from
        # canyonos test's extra_env). Set it explicitly on every agent -- to that
        # value, or empty -- so it ALWAYS wins over --env-file (docker: -e beats
        # --env-file). A user's .env can therefore neither enable the stub nor
        # change it; it is reachable only through `canyonos test`.
        cmd.extend([
            "-e",
            f"CANYONOS_LLM_STUB_TEXT={os.environ.get('CANYONOS_LLM_STUB_TEXT', '')}",
        ])

        if ctrl_type == "workflow":
            cmd.extend(["-p", f"{spec.get('api_port', 8080)}:8080"])
            config = _require_controller().config
            db_url = config.get("database", {}).get("url")
            project_id = config.get("project_id")
            if db_url:
                cmd.extend(["-e", f"CANYONOS_DATABASE_URL={db_url}"])
            if project_id:
                cmd.extend(["-e", f"CANYONOS_PROJECT_ID={project_id}"])
        if resources.get("cpu"):
            cmd.extend(["--cpus", str(resources["cpu"])])
        if resources.get("memory"):
            cmd.extend(["--memory", f"{resources['memory']}m"])
        if resources.get("gpu"):
            cmd.extend(["--gpus", str(resources["gpu"])])

        # User secrets from `env_file`. Explicit -e flags above still win, so a
        # stray CANYONOS_* line in someone's .env cannot break agent wiring.
        with env_file_args(
            _require_controller(), host, user, runtime_id, _is_local_host(host)
        ) as env_args:
            cmd.extend(env_args)
            cmd.append(image)
            result = _require_controller()._run_cmd(cmd, host, user)

        if result.returncode == 0:
            break
        if "port is already allocated" in (result.stderr or ""):
            # `docker run` leaves a `Created`-but-never-started container behind
            # under this name when the port bind fails. Remove it before
            # retrying with a new port, or the retry hits a name conflict
            # instead of the port conflict we're trying to work around.
            _require_controller()._run_cmd(["docker", "rm", "-f", runtime_id], host, user)
            host_port += 1
            continue
        raise RuntimeError(f"Failed to launch {runtime_id}: {result.stderr}")
    else:
        raise RuntimeError(
            f"Failed to launch {runtime_id}: no free port found after "
            f"{MAX_PORT_ATTEMPTS} attempts"
        )

    endpoint = f"{runtime_id}:{CONTAINER_PORT}"
    _require_controller().redis.set(f"controller:{endpoint}:agent_id", agent_id)

    instance = {
        "agent_name": agent_name,
        "provider": PROVIDER,
        "replica_index": str(replica_index),
        "host": host,
        "host_port": str(host_port),
        "container_port": str(CONTAINER_PORT),
        "endpoint": f"{host}:{host_port}",
        "redis_host": redis_host,
        "redis_port": str(spec.get("redis_port", 6379)),
        "runtime_id": runtime_id,
    }
    if user:
        instance["user"] = user
    if ctrl_type == "workflow":
        instance["api_port"] = str(spec.get("api_port", 8080))
    logger.info("Runtime ready: %s -> %s", runtime_id, instance["endpoint"])
    return instance


def terminate_instance(instance):
    runtime_id = instance.get("runtime_id")
    if not runtime_id:
        return

    result = _require_controller()._run_cmd(
        ["docker", "rm", "-f", runtime_id],
        instance.get("host", DEFAULT_HOST),
        instance.get("user"),
    )
    if result.returncode != 0:
        logger.warning("Failed to remove runtime %s", runtime_id)


def routing_endpoint_for(instance):
    return f"{instance['runtime_id']}:{CONTAINER_PORT}"
