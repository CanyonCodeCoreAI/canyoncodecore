"""
EC2 runtime helpers for Ventis.

This module is the EC2-specific backend for `provider: EC2` agents.
It does four things:
1. validate the EC2 config
2. create an EC2 instance
3. start the agent container on that instance
4. clean up the EC2 instance if startup fails

The global controller sets `_controller` before calling these helpers so
they can read config and reuse the controller's Docker/Redis logic.
"""

import socket
import time

import boto3

CONTAINER_PORT = 50051
PROVIDER = "EC2"
_controller = None

DEFAULT_SSM_DOCUMENT_NAME = "AWS-RunShellScript"
DEFAULT_SSM_TIMEOUT = 180
SSM_PENDING_STATUSES = {"Pending", "InProgress", "Delayed"}
SSM_FAILURE_STATUSES = {"Cancelled", "Cancelling", "TimedOut", "Failed"}


def _require_controller():
    if _controller is None:
        raise RuntimeError("EC2 runtime controller is not configured.")
    return _controller


def _aws_clients():
    """Return validated EC2 config, session, and clients."""
    cfg = _require_controller().config.get("ec2", {})
    required = [
        "ami_id",
        "subnet_id",
        "security_group_ids",
        "region",
    ]
    missing = [field for field in required if not cfg.get(field)]
    if missing:
        raise ValueError(f"Missing EC2 config: {', '.join(sorted(missing))}")
    if not isinstance(cfg["security_group_ids"], list) or not all(
        cfg["security_group_ids"]
    ):
        raise ValueError("EC2 security_group_ids must be a non-empty list.")

    session_kwargs = {"region_name": cfg["region"]}
    for key in (
        "profile",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    ):
        value = cfg.get(key)
        if value:
            session_kwargs["profile_name" if key == "profile" else key] = value

    session = boto3.Session(**session_kwargs)
    if not session.region_name:
        raise ValueError("EC2 region must be configured.")
    if session.get_credentials() is None:
        raise ValueError("AWS credentials are not available for the EC2 runtime.")
    client_kwargs = {"region_name": session.region_name}
    return (
        cfg,
        session,
        session.client("ec2", **client_kwargs),
        session.client("ssm", **client_kwargs),
    )


def _ec2_client():
    """Backward-compatible EC2-only client helper."""
    cfg, session, ec2_client, _ = _aws_clients()
    return cfg, session, ec2_client


def validate_config():
    """Check that the EC2 config has the fields needed to launch instances."""
    cfg, _, _, _ = _aws_clients()
    return cfg


def provision_instance(spec, replica_index, next_host_port=None):
    """Launch one EC2 instance for an agent replica and wait for its IPs."""
    cfg, _, client, _ = _aws_clients()
    agent_name = spec["name"]
    request = {
        "ImageId": cfg["ami_id"],
        "InstanceType": spec["instance_type"],
        "SubnetId": cfg["subnet_id"],
        "SecurityGroupIds": cfg["security_group_ids"],
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"ventis-{agent_name}-{replica_index}"}
                ],
            }
        ],
    }
    if cfg.get("key_name"):
        request["KeyName"] = cfg["key_name"]

    response = client.run_instances(**request)
    instance_id = response["Instances"][0]["InstanceId"]
    runtime_id = f"ventis-ec2-{agent_name.lower()}-{replica_index}--{instance_id}"
    client.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    deadline = time.time() + cfg.get("public_ip_timeout", 120)
    instance = None
    while time.time() < deadline:
        response = client.describe_instances(InstanceIds=[instance_id])
        for reservation in response.get("Reservations", []):
            for candidate in reservation.get("Instances", []):
                if candidate.get("InstanceId") == instance_id:
                    instance = candidate
                    break
            if instance:
                break
        if instance and (
            instance.get("PrivateIpAddress") or instance.get("PublicIpAddress")
        ):
            break
        time.sleep(2)

    host = (
        instance.get("PrivateIpAddress") or instance.get("PublicIpAddress")
        if instance
        else None
    )
    if not host:
        raise RuntimeError(
            f"EC2 instance {instance_id} does not have a reachable IP address."
        )

    redis_cfg = _require_controller().config.get("redis", {})
    record = {
        "host": host,
        "runtime_id": runtime_id,
        "ec2_instance_id": instance_id,
        "redis_host": host,
        "redis_port": spec.get("redis_port", redis_cfg.get("port", 6379)),
    }
    return record


def bootstrap_instance(provisioned, spec, replica_index):
    """Start the agent container on the new EC2 host and return its record."""
    cfg = validate_config()
    host = provisioned["host"]
    runtime_id = provisioned["runtime_id"]
    redis_cfg = _require_controller().config.get("redis", {})
    redis_host = provisioned.get("redis_host", host)
    redis_port = provisioned.get(
        "redis_port", spec.get("redis_port", redis_cfg.get("port", 6379))
    )

    try:
        _bootstrap_instance(
            host,
            spec,
            replica_index,
            cfg,
            instance_id=provisioned.get("ec2_instance_id"),
            redis_host=redis_host,
            redis_port=redis_port,
        )
        _check_controller_health(
            f"{host}:{CONTAINER_PORT}",
            timeout=cfg.get("controller_health_timeout", 180),
        )
        return {
            "agent_name": spec["name"],
            "provider": PROVIDER,
            "replica_index": str(replica_index),
            "host": host,
            "host_port": str(CONTAINER_PORT),
            "container_port": str(CONTAINER_PORT),
            "endpoint": f"{host}:{CONTAINER_PORT}",
            "redis_host": redis_host,
            "redis_port": str(redis_port),
            "runtime_id": runtime_id,
            "ec2_instance_id": provisioned.get("ec2_instance_id"),
        }
    except Exception:
        terminate_instance(provisioned)
        raise


def _bootstrap_instance(
    host, spec, replica_index, cfg, instance_id=None, redis_host=None, redis_port=None
):
    """Run the agent container through SSM."""
    return _bootstrap_instance_ssm(
        host,
        spec,
        replica_index,
        cfg,
        instance_id=instance_id,
        redis_host=redis_host,
        redis_port=redis_port,
    )


def _bootstrap_instance_ssm(
    host, spec, replica_index, cfg, instance_id=None, redis_host=None, redis_port=None
):
    """Run the agent container through SSM without SSH or image shipping."""
    _, _, _, ssm_client = _aws_clients()
    commands = _build_ssm_bootstrap_commands(
        host,
        spec,
        replica_index,
        cfg,
        redis_host=redis_host,
        redis_port=redis_port,
    )
    return _run_ssm_commands(
        ssm_client,
        cfg,
        instance_id=instance_id,
        commands=commands,
    )


def _build_ssm_bootstrap_commands(
    host, spec, replica_index, cfg, redis_host=None, redis_port=None
):
    agent_name = spec["name"]
    image = f"ventis-{agent_name.lower()}"
    redis_cfg = _require_controller().config.get("redis", {})
    redis_host = redis_host or redis_cfg.get("host", "localhost")
    redis_port = redis_port or spec.get("redis_port", redis_cfg.get("port", 6379))
    container_name = f"ventis-ec2-{agent_name.lower()}-{replica_index}"

    command = [
        "docker",
        "run",
        "-d",
        "-it",
        "--restart",
        "unless-stopped",
        "--add-host=host.docker.internal:host-gateway",
        "--name",
        container_name,
        "-p",
        f"{CONTAINER_PORT}:{CONTAINER_PORT}",
    ]
    command.extend(
        [
            "-e",
            f"VENTIS_REDIS_HOST={redis_host}",
            "-e",
            f"VENTIS_REDIS_PORT={redis_port}",
            "-e",
            f"VENTIS_AGENT_HOST={host}",
            "-e",
            f"VENTIS_AGENT_PORT={CONTAINER_PORT}",
            image,
        ]
    )
    commands = ["docker version", f"docker image inspect {image}", " ".join(command)]
    return commands


def _run_ssm_commands(ssm_client, cfg, instance_id, commands):
    """Send a shell command list through SSM and poll until it completes."""
    if not instance_id:
        raise ValueError("SSM bootstrap requires an EC2 instance id.")

    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName=cfg.get("ssm_document_name", DEFAULT_SSM_DOCUMENT_NAME),
        Parameters={"commands": commands},
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.time() + cfg.get("ssm_timeout", DEFAULT_SSM_TIMEOUT)

    while time.time() < deadline:
        invocation = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        status = invocation.get("Status")
        if status == "Success":
            return invocation
        if status in SSM_FAILURE_STATUSES:
            raise RuntimeError(
                f"SSM bootstrap failed for {instance_id} with status {status}: "
                f"{(invocation.get('StandardErrorContent') or '').strip()}"
            )
        if status not in SSM_PENDING_STATUSES:
            raise RuntimeError(
                f"SSM bootstrap failed for {instance_id} with unexpected status {status}."
            )
        time.sleep(2)

    raise TimeoutError(
        f"SSM bootstrap timed out for {instance_id} after {cfg.get('ssm_timeout', DEFAULT_SSM_TIMEOUT)}s."
    )


def _check_controller_health(endpoint, timeout=None):
    """Wait until the launched container accepts TCP connections."""
    host, port = endpoint.split(":")
    deadline = time.time() + (
        timeout
        or _require_controller()
        .config.get("ec2", {})
        .get("controller_health_timeout", 180)
    )
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"EC2 runtime endpoint never became reachable at {endpoint}.")


def terminate_instance(instance):
    """Delete the EC2 instance that belongs to a runtime id."""
    runtime_id = instance.get("runtime_id") if isinstance(instance, dict) else instance
    if not runtime_id or "--" not in runtime_id:
        raise ValueError(f"Invalid EC2 runtime id: {runtime_id}")

    host = instance.get("host") if isinstance(instance, dict) else None
    if host:
        getattr(_controller, "redis_containers", {}).pop(host, None)
        getattr(_controller, "node_redis", {}).pop(host, None)

    _, _, client = _ec2_client()
    client.terminate_instances(InstanceIds=[runtime_id.rsplit("--", 1)[1]])


def routing_endpoint_for(instance):
    return instance["endpoint"]
