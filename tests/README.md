# Tests
Fast suite from repo root: `python3 -m pytest tests`
Full local live smoke: `VENTIS_RUN_FULL_LOCAL=1 python3 -m unittest tests.live.test_full_local_deploy`
Small live Docker smoke: `VENTIS_RUN_LIVE_DOCKER=1 python3 -m unittest tests.live.test_local_docker_runtime`
