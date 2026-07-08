#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPO_ROOT=$(cd "$ROOT/../.." && pwd)
PROJECT_DIR="$ROOT/project"
CONFIG_PATH="$PROJECT_DIR/config/global_controller.localstack.generated.yaml"
PID_FILE="$ROOT/.deploy.pid"
LOG_FILE="$ROOT/.deploy.log"
ACTION=${1:-run}

export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-test}
export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-test}
export AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN:-test}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-1}
export VENTIS_LOCALSTACK_ENDPOINT=${VENTIS_LOCALSTACK_ENDPOINT:-http://localhost:4566}
export VENTIS_LOCALSTACK_AMI_ID=${VENTIS_LOCALSTACK_AMI_ID:-ami-0c0ffee000000001}

awsls() {
  if command -v awslocal >/dev/null 2>&1; then
    awslocal "$@"
  else
    aws --endpoint-url "$VENTIS_LOCALSTACK_ENDPOINT" "$@"
  fi
}

if [[ $ACTION == cleanup ]]; then
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" || true
  fi
  rm -f "$PID_FILE"
  instance_ids=$(awsls ec2 describe-instances --filters Name=image-id,Values="$VENTIS_LOCALSTACK_AMI_ID" Name=instance-state-name,Values=pending,running,stopping,stopped | python -c 'import json,sys; data=json.load(sys.stdin); ids=[i["InstanceId"] for r in data.get("Reservations", []) for i in r.get("Instances", [])]; print(" ".join(ids))')
  if [[ -n "$instance_ids" ]]; then
    awsls ec2 terminate-instances --instance-ids $instance_ids >/dev/null
  fi
  echo "cleanup complete"
  exit 0
fi

[[ -f "$ROOT/sandbox.env" ]] || "$ROOT/scripts/init_resources.sh"
[[ -f "$CONFIG_PATH" ]] || { echo "missing $CONFIG_PATH" >&2; exit 1; }

cd "$PROJECT_DIR"
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python -m ventis.cli build -c "$CONFIG_PATH"
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" python -m ventis.cli deploy -c "$CONFIG_PATH" >"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 90); do
  if curl -fsS http://localhost:8080/status/nope >/dev/null 2>&1 || grep -q "Deploying workflow 'main'" "$LOG_FILE" 2>/dev/null; then
    break
  fi
  sleep 2
done

if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  cat "$LOG_FILE" >&2
  exit 1
fi

python - <<'PY'
import json
import os
import socket
import subprocess
import time
import urllib.request

endpoint = os.environ["VENTIS_LOCALSTACK_ENDPOINT"]
ami_id = os.environ["VENTIS_LOCALSTACK_AMI_ID"]


def aws_json(*args):
    if subprocess.call(["bash", "-lc", "command -v awslocal >/dev/null 2>&1"]) == 0:
        cmd = ["awslocal", *args]
    else:
        cmd = ["aws", "--endpoint-url", endpoint, *args]
    return json.loads(subprocess.check_output(cmd, text=True))


instances = aws_json(
    "ec2",
    "describe-instances",
    "--filters",
    f"Name=image-id,Values={ami_id}",
    "Name=instance-state-name,Values=pending,running",
)
items = [item for reservation in instances.get("Reservations", []) for item in reservation.get("Instances", [])]
if len(items) != 2:
    raise SystemExit(f"expected 2 LocalStack EC2 instances, got {len(items)}")

instance_ids = [item["InstanceId"] for item in items]
ips = [item.get("PrivateIpAddress") or item.get("PublicIpAddress") for item in items]
if not all(ips):
    raise SystemExit(f"missing instance ips: {items}")

for _ in range(90):
    invocations = aws_json("ssm", "list-command-invocations", "--details").get("CommandInvocations", [])
    matches = [item for item in invocations if item.get("InstanceId") in instance_ids]
    if len(matches) >= 2 and all(item.get("Status") == "Success" for item in matches[:2]):
        break
    time.sleep(2)
else:
    raise SystemExit("ssm bootstrap commands did not reach Success for both instances")

for ip in ips:
    for _ in range(45):
        try:
            with socket.create_connection((ip, 50051), timeout=2):
                break
        except OSError:
            time.sleep(2)
    else:
        raise SystemExit(f"local controller never answered on {ip}:50051")

request = urllib.request.Request(
    "http://localhost:8080/main",
    data=b'{"name":"ventis"}',
    headers={"Content-Type": "application/json"},
)
request_id = json.loads(urllib.request.urlopen(request, timeout=10).read().decode())["request_id"]
for _ in range(90):
    data = json.loads(urllib.request.urlopen(f"http://localhost:8080/status/{request_id}", timeout=10).read().decode())
    if data.get("status") == "done":
        reply = data.get("result", {}).get("reply")
        if reply != "smoke:ventis":
            raise SystemExit(f"unexpected workflow reply: {data}")
        print("smoke ok")
        break
    if data.get("status") == "error":
        raise SystemExit(f"workflow failed: {data}")
    time.sleep(2)
else:
    raise SystemExit("workflow request did not finish")
PY

echo "smoke run passed"
