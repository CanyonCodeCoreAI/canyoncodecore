#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

if [[ $(uname -s) != "Linux" && ${VENTIS_LOCALSTACK_ALLOW_NON_LINUX:-0} != "1" ]]; then
  echo "This sandbox requires a Linux host." >&2
  exit 1
fi

: "${LOCALSTACK_AUTH_TOKEN:?Set LOCALSTACK_AUTH_TOKEN before starting LocalStack Pro.}"

docker compose up -d localstack >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS http://localhost:4566/_localstack/health >/dev/null; then
    echo "LocalStack is ready at http://localhost:4566"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail=200 localstack >&2
exit 1
