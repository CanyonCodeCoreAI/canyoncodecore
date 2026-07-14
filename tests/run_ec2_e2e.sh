#!/usr/bin/env bash
set -euo pipefail

# Set these values here (or override them in the invoking environment).
# These settings are intentionally explicit because this test provisions AWS resources.
: "${VENTIS_E2E_ENABLED:=1}" # Safety switch: prevents accidental real AWS provisioning.
: "${VENTIS_CONTROLLER_HOST:=44.200.194.190}"
: "${VENTIS_CONTROLLER_USER:=ubuntu}"
: "${VENTIS_CONTROLLER_PRIVATE_KEY:=~/Downloads/saakec2.pem}"
: "${VENTIS_WORKER_PRIVATE_KEY:=~/.ssh/id_ed25519}"
: "${VENTIS_AWS_REGION:=us-east-1}"
: "${VENTIS_AMI_ID:=ami-0fca83057091173ee}"
: "${VENTIS_SUBNET_ID:=subnet-084ff6499737ff809}"
: "${VENTIS_SECURITY_GROUP_IDS:=sg-0bc353412827553fd}"
: "${VENTIS_WORKER_SSH_USER:=ubuntu}"
: "${VENTIS_INSTANCE_TYPE:=t2.nano}"
export VENTIS_E2E_ENABLED VENTIS_CONTROLLER_HOST VENTIS_CONTROLLER_USER \
  VENTIS_CONTROLLER_PRIVATE_KEY VENTIS_WORKER_PRIVATE_KEY VENTIS_AWS_REGION VENTIS_AMI_ID \
  VENTIS_SUBNET_ID VENTIS_SECURITY_GROUP_IDS VENTIS_WORKER_SSH_USER \
  VENTIS_INSTANCE_TYPE

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/tests/test_ec2_e2e.py"
