#!/bin/bash
set -e

echo "==========================================="
echo "   Ventis Integration & Performance Tests"
echo "==========================================="

TEST_DIR="/tmp/ventis_test_env_$$"
PROJECT_NAME="ventis_test"

# Cleanup function ensures we kill the deployed Flask/GlobalController on exit
function cleanup {
    echo ">> Cleaning up test environment..."
    if [ -n "$DEPLOY_PID" ]; then
        kill -9 $DEPLOY_PID 2>/dev/null || true
    fi
    rm -rf "$TEST_DIR"
    echo ">> Cleanup complete."
}
trap cleanup EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

# --- Callback end-to-end test -------------------------------------------------
# Self-contained: scaffolds/builds/deploys/tears down its own stack on port 8080.
# Runs FIRST, while :8080 is free, and cleans up its own containers before the
# shared deployment below reuses the same port and container names.
echo "-------------------------------------------"
echo ">> Running Future.on_done() callback E2E test..."
python "$SCRIPT_DIR/test_callback_e2e.py" || exit 1

mkdir -p "$TEST_DIR"
cd "$TEST_DIR"

echo ">> 1. Generating new project..."
ventis new-project $PROJECT_NAME
cd $PROJECT_NAME

echo ">> 2. Building agents (ventis build)..."
ventis build

echo ">> 3. Deploying workflow (ventis deploy)..."
ventis deploy &
DEPLOY_PID=$!

# Wait for the workflow flask app to become reachable
echo ">> Waiting for deployment to become healthy on port 8080..."
until curl -s http://localhost:8080/main > /dev/null 2>&1 || [ "$?" -eq "4" ] || [ "$?" -eq "0" ] || [ "$?" -eq "52" ]; do
    sleep 2
done

# Wait an additional few seconds for agents to register to redis properly
sleep 5
echo ">> Deployment healthy! Running test suite."

echo "-------------------------------------------"
echo ">> Running Integration Tests..."
python "$SCRIPT_DIR/test_integration.py" || exit 1

echo "-------------------------------------------"
echo ">> Running Performance/Load Tests..."
python "$SCRIPT_DIR/test_performance.py" --concurrent 5 --total 20 || exit 1

echo "==========================================="
echo "   All Tests Passed Successfully!"
echo "==========================================="
