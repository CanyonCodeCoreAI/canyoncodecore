"""
Local runtime helpers for Ventis.

This module is the local-provider backend for `provider: local` agents.
It keeps the existing Docker launch/teardown behavior while letting
InstanceManager stay focused on orchestration and persistence.
"""

import logging

logger = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
CONTAINER_PORT = 50051
WORKFLOW_API_PORT = 8080
PROVIDER = "local"
_controller = None


def _require_controller():
    if _controller is None:
        raise RuntimeError("Local runtime controller is not configured.")
    return _controller


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def _container_routing_host(host):
    return "host.docker.internal" if _is_local_host(host) else host


def validate_config():
    return None


def launch_instance(spec, replica_index, next_host_port):
    host = spec.get("host", DEFAULT_HOST)
    host_port = int(spec.get("host_port", spec.get("port", next_host_port(host))))
    agent_name = spec["name"]
    agent_name = spec["name"]
    resources = spec.get("resources", {})
    ctrl_type = spec.get("type", "agent")
    image = f"ventis-{agent_name.lower()}"
    user = spec.get("user")
    redis_host = _container_routing_host(host)
    runtime_id = f"ventis-{PROVIDER}-{agent_name.lower()}-{replica_index}"

    cmd = [
        "docker",
        "run",
        "-d",
        "-it",
        "--add-host=host.docker.internal:host-gateway",
        "--name",
        runtime_id,
        "-p",
        f"{host_port}:{CONTAINER_PORT}",
        "-e",
        f"VENTIS_AGENT_PORT={host_port}",
        "-e",
        f"VENTIS_AGENT_HOST={redis_host}",
        "-e",
        f"VENTIS_REDIS_HOST={redis_host}",
        "-e",
        f"VENTIS_REDIS_PORT={spec.get('redis_port', 6379)}",
    ]
    if ctrl_type == "workflow":
        cmd.extend(["-p", f"{WORKFLOW_API_PORT}:8080"])
    if resources.get("cpu"):
        cmd.extend(["--cpus", str(resources["cpu"])])
    if resources.get("memory"):
        cmd.extend(["--memory", f"{resources['memory']}m"])
    if resources.get("gpu"):
        cmd.extend(["--gpus", str(resources["gpu"])])
    cmd.append(image)

    result = _require_controller()._run_cmd(cmd, host, user)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to launch {runtime_id}")

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
    host = instance.get("host")
    port = instance["host_port"]
    return f"{_container_routing_host(host)}:{port}"
