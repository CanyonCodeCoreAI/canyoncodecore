# Ventis tests

This directory is back to a small top-level suite:

- `test_stateful_affinity.py` — Redis routing-table and affinity checks, plus a tiny routing-snapshot regression.
- `test_integration.py` — functional script for a deployed workflow, with a few smoke tests for the script itself.
- `test_performance.py` — concurrent dispatch/poll load script, with a few smoke tests for the helpers.
- `test_runtime_ec2.py` — minimal EC2-only runtime coverage that did not exist in the original suite.

## Run the small pytest suite

```bash
pytest tests
```

## Run the full local script flow

```bash
./tests/run_tests.sh
```

## Run the scripts against an already-deployed Ventis instance

```bash
python3 tests/test_integration.py
python3 tests/test_performance.py --concurrent 10 --total 50
```
