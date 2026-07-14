#!/usr/bin/env bash
set -Eeuo pipefail

: "${EC2_HOST:?Set EC2_HOST to the target host}"
EC2_USER="${EC2_USER:-ubuntu}"
EC2_SSH_KEY="${EC2_SSH_KEY:-$HOME/.ssh/ventis_ec2}"
EC2_SSH_PORT="${EC2_SSH_PORT:-22}"
EC2_AMI_ID="${EC2_AMI_ID:-ami-00294d6141e58c157}"
EC2_SUBNET_ID="${EC2_SUBNET_ID:-subnet-0123456789abcdef0}"
EC2_SECURITY_GROUP_IDS="${EC2_SECURITY_GROUP_IDS:-sg-0123456789abcdef0}"

if [ ! -f "$EC2_SSH_KEY" ]; then
  echo "Missing SSH key: $EC2_SSH_KEY" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_TMP="$(mktemp -d)"
ARCHIVE_PATH="$LOCAL_TMP/repo.tgz"
REMOTE_ARCHIVE="/tmp/ventis-ec2-smoke-$$-$(date +%s).tgz"
TEST_START_EPOCH="$(date +%s)"
TARGET="$EC2_USER@$EC2_HOST"
SSH_OPTS=(
  -i "$EC2_SSH_KEY"
  -p "$EC2_SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=4
)
SCP_OPTS=(
  -i "$EC2_SSH_KEY"
  -P "$EC2_SSH_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=4
)

log() {
  printf '==> %s\n' "$*"
}

cleanup_local() {
  local status=$?
  rm -rf "$LOCAL_TMP"
  if [ "$status" -ne 0 ]; then
    echo "==> EC2 remote smoke test FAILED" >&2
    ssh "${SSH_OPTS[@]}" "$TARGET" "rm -f '$REMOTE_ARCHIVE'" >/dev/null 2>&1 || true
  fi
}
trap cleanup_local EXIT

log "Packing repo"
tar \
  --exclude='.git' \
  --exclude='.omx' \
  --exclude='.venv' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='docker_container' \
  --exclude='grpc_stubs' \
  --exclude='stubs' \
  -czf "$ARCHIVE_PATH" -C "$ROOT_DIR" .

log "Copying repo to $TARGET"
scp "${SCP_OPTS[@]}" "$ARCHIVE_PATH" "$TARGET:$REMOTE_ARCHIVE"

log "Running remote smoke test"
ssh "${SSH_OPTS[@]}" "$TARGET" "REMOTE_ARCHIVE='$REMOTE_ARCHIVE' TEST_START_EPOCH='$TEST_START_EPOCH' EC2_AMI_ID='$EC2_AMI_ID' EC2_SUBNET_ID='$EC2_SUBNET_ID' EC2_SECURITY_GROUP_IDS='$EC2_SECURITY_GROUP_IDS' bash -s" <<'REMOTE_EOF'
set -Eeuo pipefail

REMOTE_DIR="$(mktemp -d /tmp/ventis-ec2-smoke.XXXXXX)"
REPO_DIR="$REMOTE_DIR/repo"
BUILD_LOG="$REMOTE_DIR/build.log"
DEPLOY_LOG="$REMOTE_DIR/deploy.log"
INSTALL_LOG="$REMOTE_DIR/install.log"
DEPLOY_PID=""
WORKFLOW_HOST=""
mkdir -p "$REPO_DIR"

print_log() {
  local title=$1
  local path=$2
  if [ -f "$path" ]; then
    printf '\n---- %s (%s) ----\n' "$title" "$path"
    tail -n 200 "$path" || cat "$path"
  fi
}

cleanup_remote() {
  local status=$?
  set +e
  if [ -n "${DEPLOY_PID:-}" ]; then
    kill "$DEPLOY_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$DEPLOY_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$DEPLOY_PID" 2>/dev/null || true
  fi
  if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$REPO_DIR/.venv/bin/activate"
    cd "$REPO_DIR/ventis/templates" 2>/dev/null || true
    ventis clean >/dev/null 2>&1 || true
  fi
  if [ "$status" -ne 0 ]; then
    echo "remote ==> Smoke test FAILED" >&2
    print_log "install log" "$INSTALL_LOG"
    print_log "build log" "$BUILD_LOG"
    print_log "deploy log" "$DEPLOY_LOG"
  fi
  rm -rf "$REMOTE_DIR" "$REMOTE_ARCHIVE"
  exit "$status"
}
trap cleanup_remote EXIT

log() {
  printf 'remote ==> %s\n' "$*"
}

log "Unpacking repo"
tar -xzf "$REMOTE_ARCHIVE" -C "$REPO_DIR"
cd "$REPO_DIR"

log "Creating venv"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
export PIP_DISABLE_PIP_VERSION_CHECK=1

log "Installing repo"
python -m pip install -e . >"$INSTALL_LOG" 2>&1

cd ventis/templates
log "Writing EC2-provider config"
python3 - <<'PY'
import os
from pathlib import Path

security_groups = [
    sg.strip()
    for sg in os.environ.get("EC2_SECURITY_GROUP_IDS", "").split(",")
    if sg.strip()
]

config = f"""agents:
  - name: ExampleAgent
    replicas: 1
    redis_port: 6379
    instance_type: t2.nano
    resources:
      cpu: 1
      memory: 128
    entrypoint: agents/example_agent.py
    provider: EC2

  - name: VllmAgent
    replicas: 1
    redis_port: 6379
    instance_type: t2.nano
    resources:
      cpu: 1
      memory: 128
    entrypoint: agents/vllm_agent.py
    provider: EC2

  - name: Workflow
    replicas: 1
    type: workflow
    redis_port: 6379
    workflow_file: workflows/example_workflow.py
    provider: EC2
    instance_type: t3.micro

poll_interval: 5

redis:
  host: localhost
  port: 6379
  db: 0

ec2:
  region: us-east-1
  ami_id: {os.environ['EC2_AMI_ID']}
  subnet_id: {os.environ['EC2_SUBNET_ID']}
  security_group_ids:
"""
for sg in security_groups:
    config += f"    - {sg}\n"
config += "  ssh_user: ubuntu\n"
Path("config/global_controller.yaml").write_text(config, encoding="utf-8")
PY

log "Building"
ventis build >"$BUILD_LOG" 2>&1

log "Deploying"
ventis deploy >"$DEPLOY_LOG" 2>&1 &
DEPLOY_PID=$!

log "Waiting for workflow host assignment"
for _ in $(seq 1 90); do
  WORKFLOW_HOST="$(
    python3 - <<'PY'
import os
from datetime import datetime, timezone

import boto3

client = boto3.client("ec2", region_name="us-east-1")
start_epoch = int(os.environ["TEST_START_EPOCH"])
response = client.describe_instances(
    Filters=[
        {"Name": "tag:Name", "Values": ["ventis-Workflow-0"]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ]
)
candidates = []
for reservation in response.get("Reservations", []):
    for instance in reservation.get("Instances", []):
        if instance.get("ImageId") != os.environ["EC2_AMI_ID"]:
            continue
        launch_time = instance.get("LaunchTime")
        if not launch_time:
            continue
        if int(launch_time.timestamp()) < start_epoch:
            continue
        host = instance.get("PrivateIpAddress") or instance.get("PublicIpAddress") or ""
        if host:
            candidates.append((launch_time, host))

print(max(candidates)[1] if candidates else "")
PY
  )"
  if [ -n "$WORKFLOW_HOST" ]; then
    break
  fi
  if ! kill -0 "$DEPLOY_PID" 2>/dev/null; then
    echo "ventis deploy exited before the workflow host was assigned" >&2
    exit 1
  fi
  sleep 2
done

if [ -z "$WORKFLOW_HOST" ]; then
  echo "Timed out waiting for workflow host assignment in Redis" >&2
  exit 1
fi

WORKFLOW_BASE_URL="http://$WORKFLOW_HOST:8080"
log "Waiting for workflow endpoint at $WORKFLOW_BASE_URL/main"
ready=''
for _ in $(seq 1 60); do
  http_code="$(curl -s -o /dev/null -w '%{http_code}' "$WORKFLOW_BASE_URL/main" || true)"
  case "$http_code" in
    200|202|400|404|405)
      ready=1
      break
      ;;
  esac
  if ! kill -0 "$DEPLOY_PID" 2>/dev/null; then
    echo "ventis deploy exited before the workflow became reachable" >&2
    exit 1
  fi
  sleep 2
done

if [ -z "$ready" ]; then
  echo "Timed out waiting for $WORKFLOW_BASE_URL/main" >&2
  exit 1
fi

sleep 5

log "Submitting workflow request"
submit_body="$REMOTE_DIR/submit.json"
submit_code="$(curl -sS -o "$submit_body" -w '%{http_code}' \
  -X POST "$WORKFLOW_BASE_URL/main" \
  -H 'Content-Type: application/json' \
  -d '{"name":"World"}')"

if [ "$submit_code" != "202" ]; then
  echo "Unexpected submit status: $submit_code" >&2
  cat "$submit_body" >&2
  exit 1
fi

REQUEST_ID="$(python - "$submit_body" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    data = json.load(fh)
request_id = data.get('request_id')
if not request_id:
    raise SystemExit('Missing request_id in submit response')
print(request_id)
PY
)"

log "Polling status for $REQUEST_ID"
python3 - "$REQUEST_ID" "$WORKFLOW_BASE_URL" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

request_id = sys.argv[1]
workflow_base_url = sys.argv[2]
url = f"{workflow_base_url}/status/{request_id}"
last = None
for _ in range(60):
    try:
        with urlopen(url, timeout=5) as res:
            payload = json.load(res)
    except URLError as exc:
        last = f"status request failed: {exc}"
        time.sleep(1)
        continue

    status = payload.get("status")
    last = payload
    if status == "done":
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SystemExit(f"Missing result object: {payload}")
        greeting = result.get("greeting")
        if not greeting or "World" not in greeting:
            raise SystemExit(f"Unexpected greeting: {payload}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        raise SystemExit(0)
    if status == "error":
        raise SystemExit(f"Workflow returned error: {payload}")
    time.sleep(1)

raise SystemExit(f"Timed out waiting for completion: {last}")
PY

log "Smoke test passed"
REMOTE_EOF

log "EC2 remote smoke test passed"
