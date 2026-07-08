#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

AMI_ID=${VENTIS_LOCALSTACK_AMI_ID:-ami-0c0ffee000000001}
AMI_NAME=${VENTIS_LOCALSTACK_AMI_NAME:-ventis-localstack-ssm-ami}
BUILD_TAG=${AMI_NAME}:build
FINAL_TAG=localstack-ec2/${AMI_NAME}:${AMI_ID}

docker build -t "$BUILD_TAG" "$ROOT/ami"
docker tag "$BUILD_TAG" "$FINAL_TAG"
docker image inspect "$FINAL_TAG" >/dev/null

echo "$FINAL_TAG"
