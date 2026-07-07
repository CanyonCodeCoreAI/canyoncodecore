# Ventis Testing & Load Analysis Tools

This directory contains an automated end-to-end testing suite for Ventis. It is designed to verify both functional correctness and concurrent performance of the distributed agent architecture.

## 1. Automated Test Runner (`run_tests.sh`)
This script automates the entire testing lifecycle by interacting with the `ventis` CLI:
1. Scaffolds a new temporary project using `ventis new-project`.
2. Compiles the project using `ventis build`.
3. Launches the project using `ventis deploy` in the background.
4. Waits for the GlobalController and all agent sidecars to become healthy.
5. Runs the Python integration and performance scripts.
6. **Cleanup:** Automatically terminates the deployment and cleans up the temporary directory upon success or failure.

To run the complete suite:
```bash
./run_tests.sh
```

## 2. Functional Integration Validation (`test_integration.py`)
Verifies that Ventis correctly passes data and dependencies between chained agents. 
- Dispatches a single query to the deployed `/main` endpoint.
- Polls the `/status` endpoint until completion.
- Validates the output payload structure and ensures that data successfully flowed through `FinanceAgent`, `MarketResearchAgent`, and `VllmAgent`.

To run manually against an already-deployed Ventis instance:
```bash
python test_integration.py
```

## 3. Callback End-to-End Test (`test_callback_e2e.py`)
A self-contained regression test for `Future.on_done()` callbacks. Unlike the
other scripts, it owns the entire lifecycle: it scaffolds a temp project, swaps
in a workflow that exercises callbacks, then builds, deploys, fires a request,
asserts, and tears everything down.

It verifies three behaviors through the full stack (gRPC → LocalController →
agent container → Redis → local watcher thread):
- **Deferred fire** — callback registered before the result is ready; the watcher thread fires it once the result lands.
- **Immediate fire** — callback registered after `.value()` cached the result; fires synchronously.
- **Ordering** — multiple callbacks fire in registration order.

Run standalone (requires Docker running + `pip install -e .`):
```bash
python test_callback_e2e.py
```

## 4. High-Concurrency Stress Test (`test_performance.py`)
Evaluates the robustness and scalability of the Ventis Redis routing and Docker architecture under load. Using `concurrent.futures`, this script models N concurrent users actively polling Ventis simultaneously.

It produces an analytical report summarizing throughput, dropped requests, and latency percentiles.

To run manually against an already-deployed Ventis instance (e.g. 50 requests across 10 concurrent virtual users):
```bash
python test_performance.py --concurrent 10 --total 50
```
