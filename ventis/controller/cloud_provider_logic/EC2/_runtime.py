import logging
import socket
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

CONTAINER_PORT = 50051
_controller = None
DEFAULT_REMOTE_PROJECT_DIR = "/opt/ventis/project"
DEFAULT_AGENT_IMAGE = "ventis-agent-base"


def _set_controller(controller):
    global _controller
    _controller = controller


def _require_controller():
    if _controller is None:
        raise RuntimeError("EC2 runtime controller is not configured.")
    return _controller


def _ec2_config():
    return _require_controller().config.get("ec2", {})


def _redis_config():
    return _require_controller().config.get("redis", {})


def _session():
    cfg = _ec2_config()
    kwargs = {"region_name": cfg.get("region")}
    if cfg.get("profile"):
        kwargs["profile_name"] = cfg["profile"]
    if cfg.get("aws_access_key_id"):
        kwargs["aws_access_key_id"] = cfg["aws_access_key_id"]
    if cfg.get("aws_secret_access_key"):
        kwargs["aws_secret_access_key"] = cfg["aws_secret_access_key"]
    if cfg.get("aws_session_token"):
        kwargs["aws_session_token"] = cfg["aws_session_token"]
    return boto3.Session(**kwargs)


def _ec2_client():
    return _session().client("ec2", region_name=_ec2_config()["region"])


def _instance_id_from_runtime_id(runtime_id):
    if "--" not in runtime_id:
        raise ValueError(f"Invalid EC2 runtime id: {runtime_id}")
    return runtime_id.rsplit("--", 1)[1]


def _describe_instance(instance_id):
    response = _ec2_client().describe_instances(InstanceIds=[instance_id])
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            if instance.get("InstanceId") == instance_id:
                return instance
    return None


def _preferred_instance_host(instance):
    if not instance:
        return None
    return instance.get("PrivateIpAddress") or instance.get("PublicIpAddress")


def _ssh_instance_host(instance):
    if not instance:
        return None
    return instance.get("PublicIpAddress") or instance.get("PrivateIpAddress")


def _instance_name(agent_name, replica_index):
    return f"ventis-ec2-{agent_name.lower()}-{replica_index}"


def _runtime_id(agent_name, replica_index, instance_id):
    return f"ventis-ec2-{agent_name.lower()}-{replica_index}--{instance_id}"

def validate_config():
    cfg = _ec2_config()
    required = [
        "ami_id",
        "instance_type",
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

    session = _session()
    if not session.region_name:
        raise ValueError("EC2 region must be configured.")

    credentials = session.get_credentials()
    if credentials is None:
        raise ValueError("AWS credentials are not available for the EC2 runtime.")

    _ec2_client()
    return cfg


def create_instance(spec, replica_index):
    provisioned = provision_instance(spec, replica_index)
    return bootstrap_instance(provisioned, spec, replica_index)


def provision_instance(spec, replica_index):
    cfg = validate_config()
    client = _ec2_client()
    agent_name = spec["name"]
    tags = [
        {"Key": "Name", "Value": f"ventis-{agent_name}-{replica_index}"},
        {"Key": "VentisManaged", "Value": "true"},
        {"Key": "VentisProvider", "Value": "EC2"},
        {"Key": "VentisAgent", "Value": agent_name},
        {"Key": "VentisReplica", "Value": str(replica_index)},
    ]
    request = {
        "ImageId": cfg["ami_id"],
        "InstanceType": cfg["instance_type"],
        "SubnetId": cfg["subnet_id"],
        "SecurityGroupIds": cfg["security_group_ids"],
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags + [{"Key": "CreatedBy", "Value": "EC2 Fast Launch"}]},
            {"ResourceType": "volume", "Tags": [{"Key": "CreatedBy", "Value": "EC2 Fast Launch"}]},
        ],
    }
    if cfg.get("key_name"):
        request["KeyName"] = cfg["key_name"]

    try:
        response = client.run_instances(**request)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "UnauthorizedOperation":
            raise RuntimeError(
                "EC2 launch failed: controller IAM role is missing ec2:RunInstances "
                "(and likely related EC2 permissions such as DescribeInstances and CreateTags)."
            ) from exc
        raise
    instance_id = response["Instances"][0]["InstanceId"]
    runtime_id = _runtime_id(agent_name, replica_index, instance_id)
    instance = _wait_for_instance_ready(runtime_id)
    host = _preferred_instance_host(instance)
    ssh_host = _ssh_instance_host(instance)
    if not host:
        raise RuntimeError(f"EC2 instance {instance_id} does not have a reachable IP address.")
    if not ssh_host:
        raise RuntimeError(f"EC2 instance {instance_id} does not have an SSH-reachable IP address.")
    redis_cfg = _redis_config()
    return {
        "host": host,
        "ssh_host": ssh_host,
        "runtime_id": runtime_id,
        "ec2_instance_id": instance_id,
        "user": cfg["ssh_user"],
        "redis_port": spec.get("redis_port", redis_cfg.get("port", 6379)),
    }


def bootstrap_instance(provisioned, spec, replica_index, redis_host=None, redis_port=None):
    host = provisioned["host"]
    ssh_host = provisioned.get("ssh_host", host)
    runtime_id = provisioned["runtime_id"]
    try:
        _bootstrap_instance(
            host,
            spec,
            replica_index,
            ssh_host=ssh_host,
            redis_host=redis_host,
            redis_port=redis_port,
        )
        endpoint = f"{host}:{CONTAINER_PORT}"
        _check_controller_health(endpoint)
        return _build_instance_record(
            spec,
            replica_index,
            host,
            runtime_id,
            redis_host=redis_host,
            redis_port=redis_port,
            ec2_instance_id=provisioned.get("ec2_instance_id"),
        )
    except Exception:
        logger.exception("EC2 runtime bootstrap failed for %s", runtime_id)
        try:
            terminate_instance(runtime_id)
        except Exception:
            logger.warning("Leaving failed EC2 instance %s running for manual cleanup.", runtime_id)
        raise


def _wait_for_instance_ready(runtime_id):
    instance_id = _instance_id_from_runtime_id(runtime_id)
    client = _ec2_client()
    client.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    deadline = time.time() + _ec2_config().get("public_ip_timeout", 120)
    while time.time() < deadline:
        instance = _describe_instance(instance_id)
        if _preferred_instance_host(instance) and _ssh_instance_host(instance):
            return instance
        time.sleep(2)
    raise TimeoutError(f"EC2 instance {instance_id} never received usable network addresses.")


def _get_instance_host(runtime_id):
    instance_id = _instance_id_from_runtime_id(runtime_id)
    instance = _describe_instance(instance_id)
    host = _preferred_instance_host(instance)
    if not host:
        raise RuntimeError(f"EC2 instance {instance_id} does not have a usable runtime IP.")
    return host


def _bootstrap_instance(host, spec, replica_index, ssh_host=None, redis_host=None, redis_port=None):
    cfg = _ec2_config()
    controller = _require_controller()
    agent_name = spec["name"]
    ctrl_type = spec.get("type", "agent")
    entrypoint = spec.get("entrypoint")
    use_generic_agent_image = ctrl_type != "workflow" and bool(entrypoint)
    image = cfg.get("agent_image", DEFAULT_AGENT_IMAGE) if use_generic_agent_image else f"ventis-{agent_name.lower()}"
    remote_project_dir = cfg.get("remote_project_dir", DEFAULT_REMOTE_PROJECT_DIR)
    redis_cfg = _redis_config()
    ssh_host = ssh_host or host
    redis_host = redis_host or redis_cfg.get("host", "localhost")
    redis_port = redis_port or spec.get("redis_port", redis_cfg.get("port", 6379))

    prep_result = controller._ensure_remote_docker(ssh_host, cfg["ssh_user"])
    if prep_result.returncode != 0:
        raise RuntimeError(f"Failed to prepare Docker on {ssh_host}: {prep_result.stderr.strip()}")
    if use_generic_agent_image:
        controller._sync_project_to_host(ssh_host, cfg["ssh_user"], remote_project_dir)
    controller._ensure_image_on_host(image, ssh_host, cfg["ssh_user"])

    cmd = [
        "docker",
        "run",
        "-d",
        "-it",
        "--restart",
        "unless-stopped",
        "--add-host=host.docker.internal:host-gateway",
        "--name",
        _instance_name(agent_name, replica_index),
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
        "-e",
        f"VENTIS_AGENT_PRIORITY={spec.get('priority', 0)}",
    ]
    if use_generic_agent_image:
        cmd.extend([
            "-w",
            "/workspace",
            "-v",
            f"{remote_project_dir}:/workspace",
            "-e",
            f"VENTIS_AGENT_NAME={agent_name}",
            "-e",
            f"VENTIS_AGENT_FILE={entrypoint}",
        ])
    cmd.append(image)
    result = controller._run_cmd(cmd, ssh_host, cfg["ssh_user"])
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to launch EC2 runtime container for {agent_name} on {ssh_host}: {result.stderr.strip()}"
        )
    return result


def _check_controller_health(endpoint):
    host, port = endpoint.split(":")
    deadline = time.time() + _ec2_config().get("controller_health_timeout", 180)
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"LocalController never became reachable at {endpoint}.")


def _build_instance_record(
    spec,
    replica_index,
    host,
    runtime_id,
    redis_host=None,
    redis_port=None,
    ec2_instance_id=None,
):
    redis_cfg = _redis_config()
    redis_host = redis_host or redis_cfg.get("host", "localhost")
    redis_port = redis_port or spec.get("redis_port", redis_cfg.get("port", 6379))
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
        "ec2_instance_id": ec2_instance_id or _instance_id_from_runtime_id(runtime_id),
    }


def terminate_instance(runtime_id):
    instance_id = _instance_id_from_runtime_id(runtime_id)
    client = _ec2_client()
    try:
        client.terminate_instances(InstanceIds=[instance_id])
    except Exception:
        logger.exception("Failed to terminate EC2 instance %s", instance_id)
