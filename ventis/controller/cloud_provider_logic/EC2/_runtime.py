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

import base64
import os
import socket
import subprocess
import time

import boto3

from ventis.utils.redis_client import RedisClient

CONTAINER_PORT = 50051
DEFAULT_SSH_KEY_PATH = os.path.expanduser("~/.ssh/ventis_ec2")
_controller = None


def _aws_clients():
    """Return validated EC2 config and EC2 client."""
    cfg = _controller.config.get("ec2", {})
    required = [
        "ami_id",
        "subnet_id",
        "security_group_ids",
        "region",
        "ssh_user",
    ]
    missing = [field for field in required if not cfg.get(field)]
    if missing:
        raise ValueError(f"Missing EC2 config: {', '.join(sorted(missing))}")
    return cfg, boto3.client("ec2", region_name=cfg["region"])


def _ensure_ssh_keypair():
    """Create ~/.ssh/ventis_ec2 if missing and return the public key."""
    private = DEFAULT_SSH_KEY_PATH
    public = private + ".pub"
    if not os.path.exists(private):
        key_dir = os.path.dirname(private)
        if key_dir:
            os.makedirs(key_dir, exist_ok=True)
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                private,
                "-N",
                "",
                "-C",
                "ventis-ec2",
            ],
            check=True,
            capture_output=True,
        )
    with open(public) as f:
        return f.read().strip()


def _userdata(ssh_user, pubkey):
    """Build base64 UserData that installs the public key on the worker."""
    script = (
        "#!/bin/bash\n"
        f"mkdir -p /home/{ssh_user}/.ssh\n"
        f"echo '{pubkey}' >> /home/{ssh_user}/.ssh/authorized_keys\n"
        f"chown -R {ssh_user}:{ssh_user} /home/{ssh_user}/.ssh\n"
        f"chmod 700 /home/{ssh_user}/.ssh\n"
        f"chmod 600 /home/{ssh_user}/.ssh/authorized_keys\n"
    )
    return base64.b64encode(script.encode()).decode()


def provision_instance(spec, replica_index, next_host_port=None):
    """Launch one EC2 instance for an agent replica and wait for its IPs."""
    cfg, client = _aws_clients()
    pubkey = _ensure_ssh_keypair()
    agent_name = spec["name"]
    request = {
        "ImageId": cfg["ami_id"],
        "InstanceType": spec["instance_type"],
        "SubnetId": cfg["subnet_id"],
        "SecurityGroupIds": cfg["security_group_ids"],
        "UserData": _userdata(cfg["ssh_user"], pubkey),
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"ventis-{agent_name}-{replica_index}"},
                    {"Key": "CreatedBy", "Value": "EC2 Fast Launch"},
                ],
            },
            {
                "ResourceType": "volume",
                "Tags": [
                    {
                        "Key": "CreatedBy",
                        "Value": "EC2 Fast Launch",
                    }
                ],
            },
        ],
    }

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

    redis_port = spec.get(
        "redis_port", _controller.config.get("redis", {}).get("port", 6379)
    )
    record = {
        "host": host,
        "runtime_id": runtime_id,
        "ec2_instance_id": instance_id,
        "redis_host": host,
        "redis_port": redis_port,
    }
    return record


def bootstrap_instance(provisioned, spec, replica_index):
    """Start the agent container on the new EC2 host and return its record."""
    cfg, _ = _aws_clients()
    host = provisioned["host"]
    runtime_id = provisioned["runtime_id"]
    redis_host = provisioned["redis_host"]
    redis_port = provisioned["redis_port"]

    try:
        _bootstrap_instance(
            host,
            spec,
            replica_index,
            cfg,
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
            "redis_host": redis_host,
            "redis_port": str(redis_port),
            "runtime_id": runtime_id,
            "ec2_instance_id": provisioned.get("ec2_instance_id"),
        }
    except Exception:
        terminate_instance(provisioned)
        raise


def _bootstrap_instance(host, spec, replica_index, cfg, redis_host, redis_port):
    """Run the agent container over SSH."""
    ssh_user = cfg["ssh_user"]

    for _ in range(30):
        result = _controller._run_cmd(["true"], host, user=ssh_user)
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"SSH never became ready on {host}")

    redis_container = f"ventis-redis-{host.replace('.', '-')}"
    result = _controller._run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            redis_container,
            "-p",
            f"{redis_port}:6379",
            "redis:alpine",
        ],
        host,
        user=ssh_user,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start Redis on {host}: {(result.stderr or result.stdout or '').strip()}"
        )
    getattr(_controller, "redis_containers", {})[host] = redis_container
    getattr(_controller, "node_redis", {})[host] = RedisClient(
        host=host, port=int(redis_port)
    )

    agent_name = spec["name"]
    image = f"ventis-{agent_name.lower()}"
    container_name = f"ventis-ec2-{agent_name.lower()}-{replica_index}"
    key = os.path.expanduser("~/.ssh/ventis_ec2")
    port_args = ["-p", f"{CONTAINER_PORT}:{CONTAINER_PORT}"]
    if spec.get("type") == "workflow":
        port_args += ["-p", f"{spec.get('api_port', 8080)}:8080"]

    result = subprocess.run(
        f"docker save {image} | ssh -o StrictHostKeyChecking=no -i {key} "
        f"{ssh_user}@{host} 'sudo docker load'",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to transfer image to {host}: {result.stderr}")

    cmd = [
        "docker",
        "run",
        "-d",
        "-it",
        "--restart",
        "unless-stopped",
        "--add-host=host.docker.internal:host-gateway",
        "--name",
        container_name,
        *port_args,
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
    result = _controller._run_cmd(cmd, host, user=ssh_user)
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH bootstrap failed on {host}: {(result.stderr or result.stdout or '').strip()}"
        )


def _check_controller_health(endpoint, timeout=None):
    """Wait until the launched container accepts TCP connections."""
    host, port = endpoint.split(":")
    deadline = time.time() + (
        timeout
        or _controller.config.get("ec2", {}).get("controller_health_timeout", 180)
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

    _, client = _aws_clients()
    client.terminate_instances(InstanceIds=[runtime_id.rsplit("--", 1)[1]])


def routing_endpoint_for(instance):
    """Return the gRPC endpoint string used for routing to this instance."""
    return instance["endpoint"]
