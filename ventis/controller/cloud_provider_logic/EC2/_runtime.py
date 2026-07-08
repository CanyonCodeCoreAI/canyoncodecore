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
_controller = None


def _ec2_client():
    """Return validated EC2 config, session, and client."""
    if _controller is None:
        raise RuntimeError("EC2 runtime controller is not configured.")

    cfg = _controller.config.get("ec2", {})
    required = [
        "ami_id",
        "subnet_id",
        "security_group_ids",
        "ssh_user",
        "region",
    ]
    missing = [field for field in required if not cfg.get(field)]
    if missing:
        raise ValueError(f"Missing EC2 config: {', '.join(sorted(missing))}")
    if not isinstance(cfg["security_group_ids"], list) or not all(cfg["security_group_ids"]):
        raise ValueError("EC2 security_group_ids must be a non-empty list.")

    session_kwargs = {"region_name": cfg["region"]}
    for key in ("profile", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
        value = cfg.get(key)
        if value:
            session_kwargs["profile_name" if key == "profile" else key] = value

    session = boto3.Session(**session_kwargs)
    if not session.region_name:
        raise ValueError("EC2 region must be configured.")
    if session.get_credentials() is None:
        raise ValueError("AWS credentials are not available for the EC2 runtime.")

    return cfg, session, session.client("ec2", region_name=session.region_name)


def validate_config():
    """Check that the EC2 config has the fields needed to launch instances."""
    cfg, _, _ = _ec2_client()
    return cfg


def provision_instance(spec, replica_index):
    """Launch one EC2 instance for an agent replica and wait for its IPs."""
    cfg, _, client = _ec2_client()
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
                "Tags": [{"Key": "Name", "Value": f"ventis-{agent_name}-{replica_index}"}],
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
        if instance and (instance.get("PrivateIpAddress") or instance.get("PublicIpAddress")):
            break
        time.sleep(2)

    host = instance.get("PrivateIpAddress") or instance.get("PublicIpAddress") if instance else None
    ssh_host = instance.get("PublicIpAddress") or instance.get("PrivateIpAddress") if instance else None
    if not host:
        raise RuntimeError(f"EC2 instance {instance_id} does not have a reachable IP address.")
    if not ssh_host:
        raise RuntimeError(f"EC2 instance {instance_id} does not have an SSH-reachable IP address.")

    redis_cfg = _controller.config.get("redis", {})
    return {
        "host": host,
        "ssh_host": ssh_host,
        "runtime_id": runtime_id,
        "ec2_instance_id": instance_id,
        "user": cfg["ssh_user"],
        "redis_port": spec.get("redis_port", redis_cfg.get("port", 6379)),
    }


def bootstrap_instance(provisioned, spec, replica_index, redis_host=None, redis_port=None):
    """Start the agent container on the new EC2 host and return its record."""
    cfg = validate_config()
    host = provisioned["host"]
    runtime_id = provisioned["runtime_id"]
    redis_cfg = _controller.config.get("redis", {})

    try:
        _bootstrap_instance(
            host,
            spec,
            replica_index,
            cfg,
            ssh_host=provisioned.get("ssh_host", host),
            redis_host=redis_host,
            redis_port=redis_port,
        )
        _check_controller_health(
            f"{host}:{CONTAINER_PORT}",
            timeout=cfg.get("controller_health_timeout", 180),
        )
        return {
            "agent_name": spec["name"],
            "provider": "EC2",
            "replica_index": str(replica_index),
            "host": host,
            "host_port": str(CONTAINER_PORT),
            "container_port": str(CONTAINER_PORT),
            "endpoint": f"{host}:{CONTAINER_PORT}",
            "redis_host": redis_host or redis_cfg.get("host", "localhost"),
            "redis_port": str(redis_port or spec.get("redis_port", redis_cfg.get("port", 6379))),
            "runtime_id": runtime_id,
            "ec2_instance_id": provisioned.get("ec2_instance_id"),
        }
    except Exception:
        terminate_instance(runtime_id)
        raise


def _bootstrap_instance(host, spec, replica_index, cfg, ssh_host=None, redis_host=None, redis_port=None):
    """Prepare the remote host, ship the image, and run the agent container."""
    agent_name = spec["name"]
    ssh_user = cfg["ssh_user"]
    image = f"ventis-{agent_name.lower()}"
    redis_cfg = _controller.config.get("redis", {})
    ssh_host = ssh_host or host
    redis_host = redis_host or redis_cfg.get("host", "localhost")
    redis_port = redis_port or spec.get("redis_port", redis_cfg.get("port", 6379))

    prep_result = _controller._ensure_remote_docker(ssh_host, ssh_user)
    if prep_result.returncode != 0:
        raise RuntimeError(f"Failed to prepare Docker on {ssh_host}: {prep_result.stderr.strip()}")
    if _controller.registry_url:
        _controller._ship_image_registry(image, ssh_host, ssh_user)
    else:
        _controller._ship_image_ssh(image, ssh_host, ssh_user)

    result = _controller._run_cmd(
        [
            "docker",
            "run",
            "-d",
            "-it",
            "--restart",
            "unless-stopped",
            "--add-host=host.docker.internal:host-gateway",
            "--name",
            f"ventis-ec2-{agent_name.lower()}-{replica_index}",
            "-p",
            f"{CONTAINER_PORT}:{CONTAINER_PORT}",
            "-e",
            f"VENTIS_REDIS_HOST={redis_host}",
            "-e",
            f"VENTIS_REDIS_PORT={redis_port}",
            "-e",
            f"VENTIS_AGENT_HOST={host}",
            "-e",
            f"VENTIS_AGENT_PORT={CONTAINER_PORT}",
            image,
        ],
        ssh_host,
        ssh_user,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to launch EC2 runtime container for {agent_name} on {ssh_host}: {result.stderr.strip()}"
        )
    return result


def _check_controller_health(endpoint, timeout=None):
    """Wait until the launched container accepts TCP connections."""
    host, port = endpoint.split(":")
    deadline = time.time() + (timeout or _controller.config.get("ec2", {}).get("controller_health_timeout", 180))
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"EC2 runtime endpoint never became reachable at {endpoint}.")


def terminate_instance(runtime_id):
    """Delete the EC2 instance that belongs to a runtime id."""
    if "--" not in runtime_id:
        raise ValueError(f"Invalid EC2 runtime id: {runtime_id}")
    _, _, client = _ec2_client()
    client.terminate_instances(InstanceIds=[runtime_id.rsplit("--", 1)[1]])
