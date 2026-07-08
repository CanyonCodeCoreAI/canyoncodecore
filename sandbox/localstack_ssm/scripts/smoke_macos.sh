#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_ROOT=$(cd "$ROOT/../.." && pwd)
PROJECT_DIR="$ROOT/project"
CONFIG_PATH="$PROJECT_DIR/config/global_controller.localstack.generated.yaml"
REDIS_CONTAINER=ventis-macos-redis
REDIS_PORT=6389
ACTION=${1:-run}
export REDIS_CONTAINER REDIS_PORT

if [[ $(uname -s) != "Darwin" ]]; then
  echo "This script is only for macOS." >&2
  exit 1
fi

export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-test}
export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-test}
export AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN:-test}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-1}
export VENTIS_LOCALSTACK_ENDPOINT=${VENTIS_LOCALSTACK_ENDPOINT:-http://localhost:4566}
export VENTIS_LOCALSTACK_AMI_ID=${VENTIS_LOCALSTACK_AMI_ID:-ami-0c0ffee000000001}
export VENTIS_LOCALSTACK_ALLOW_NON_LINUX=1

awsls() {
  if command -v awslocal >/dev/null 2>&1; then
    awslocal "$@"
  else
    aws --endpoint-url "$VENTIS_LOCALSTACK_ENDPOINT" "$@"
  fi
}

if [[ $ACTION == cleanup ]]; then
  docker rm -f "$REDIS_CONTAINER" >/dev/null 2>&1 || true
  instance_ids=$(awsls ec2 describe-instances --filters Name=image-id,Values="$VENTIS_LOCALSTACK_AMI_ID" Name=instance-state-name,Values=pending,running,stopping,stopped | python3 -c 'import json,sys; data=json.load(sys.stdin); ids=[i["InstanceId"] for r in data.get("Reservations", []) for i in r.get("Instances", [])]; print(" ".join(ids))')
  if [[ -n "$instance_ids" ]]; then
    awsls ec2 terminate-instances --instance-ids $instance_ids >/dev/null
  fi
  docker rm -f ventis-ec2-smokeagent-0 ventis-ec2-smokeagent-1 >/dev/null 2>&1 || true
  echo "macOS cleanup complete"
  exit 0
fi

"$ROOT/scripts/build_ami.sh" >/dev/null
"$ROOT/scripts/init_resources.sh" >/dev/null
set -a
source "$ROOT/sandbox.env"
set +a
[[ -f "$CONFIG_PATH" ]] || { echo "missing $CONFIG_PATH" >&2; exit 1; }

docker rm -f ventis-ec2-smokeagent-0 ventis-ec2-smokeagent-1 >/dev/null 2>&1 || true
docker rm -f "$REDIS_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$REDIS_CONTAINER" -p "$REDIS_PORT:6379" redis:alpine >/dev/null

cd "$PROJECT_DIR"
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m ventis.cli build -c "$CONFIG_PATH" >/dev/null

cd "$REPO_ROOT"
python3 - <<'PY'
import os
import time
from types import SimpleNamespace

import boto3

from ventis.controller.cloud_provider_logic.EC2 import _runtime as ec2_runtime

endpoint = os.environ["VENTIS_LOCALSTACK_ENDPOINT"]
session = boto3.Session(
    region_name=os.environ["AWS_DEFAULT_REGION"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    aws_session_token=os.environ["AWS_SESSION_TOKEN"],
)
ec2 = session.client("ec2", region_name=session.region_name, endpoint_url=endpoint)
ssm = session.client("ssm", region_name=session.region_name, endpoint_url=endpoint)
subnet_id = os.environ["VENTIS_LOCALSTACK_SUBNET_ID"]
security_group_id = os.environ["VENTIS_LOCALSTACK_SECURITY_GROUP_ID"]
ami_id = os.environ["VENTIS_LOCALSTACK_AMI_ID"]
redis_port = int(os.environ["REDIS_PORT"])

cfg = {
    "endpoint_url": endpoint,
    "ssm_document_name": "AWS-RunShellScript",
    "ssm_timeout": 180,
}
ec2_runtime._controller = SimpleNamespace(
    config={"redis": {"host": "host.docker.internal", "port": redis_port}, "ec2": cfg},
    registry_url=None,
)

instances = []
for replica_index in range(2):
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType="m1.small",
        SubnetId=subnet_id,
        SecurityGroupIds=[security_group_id],
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": f"ventis-SmokeAgent-{replica_index}"}],
        }],
    )
    instance_id = response["Instances"][0]["InstanceId"]
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    deadline = time.time() + 120
    host = None
    while time.time() < deadline:
        reservations = ec2.describe_instances(InstanceIds=[instance_id]).get("Reservations", [])
        for reservation in reservations:
            for candidate in reservation.get("Instances", []):
                host = candidate.get("PrivateIpAddress") or candidate.get("PublicIpAddress")
                if host:
                    break
            if host:
                break
        if host:
            break
        time.sleep(2)
    if not host:
        raise SystemExit(f"instance {instance_id} never got an IP")

    commands = ec2_runtime._build_ssm_bootstrap_commands(
        host,
        {"name": "SmokeAgent", "provider": "EC2", "redis_port": redis_port},
        replica_index,
        cfg,
        redis_host="host.docker.internal",
        redis_port=redis_port,
    )
    ec2_runtime._run_ssm_commands(ssm, cfg, instance_id, commands)
    instances.append((replica_index, instance_id))

print("bootstrapped", instances)
PY

python3 - <<'PY'
import json
import os
import subprocess
import time

redis_container = os.environ["REDIS_CONTAINER"]
expected = [
    ("ventis-ec2-smokeagent-0", "localstack-ec2.i-"),
    ("ventis-ec2-smokeagent-1", "localstack-ec2.i-"),
]

for name, network_prefix in expected:
    inspect = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]
    if not inspect["State"]["Running"]:
        raise SystemExit(f"{name} is not running")
    network_mode = inspect["HostConfig"]["NetworkMode"]
    if not network_mode.startswith(f"container:{network_prefix}"):
        raise SystemExit(f"{name} network mode mismatch: {network_mode}")

deadline = time.time() + 60
healthy = []
while time.time() < deadline:
    keys = subprocess.check_output(
        ["docker", "exec", redis_container, "redis-cli", "--raw", "keys", "controller:*:status"],
        text=True,
    ).splitlines()
    healthy = []
    for key in keys:
        value = subprocess.check_output(
            ["docker", "exec", redis_container, "redis-cli", "--raw", "get", key],
            text=True,
        ).strip()
        if value == "healthy":
            healthy.append(key)
    if len(healthy) >= 2:
        break
    time.sleep(2)

if len(healthy) < 2:
    raise SystemExit(f"expected 2 healthy controller status keys, got {healthy}")

print("macOS smoke ok")
PY

echo "macOS bootstrap smoke passed"
