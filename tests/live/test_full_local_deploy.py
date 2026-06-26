import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import yaml

from ventis.utils.redis_client import RedisClient


RUN_FULL_LOCAL = os.environ.get("VENTIS_RUN_FULL_LOCAL") == "1"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _docker_available():
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "ps"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _run_ventis(args, cwd, timeout=180):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "ventis.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _request_json(method, url, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _wait_for_http(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _request_json("POST", url, {"name": "Probe"}, timeout=2)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1)
    raise TimeoutError(f"{url} did not become reachable within {timeout}s")


def _wait_for_done(request_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _status, payload = _request_json(
            "GET",
            f"http://localhost:8080/status/{request_id}",
            timeout=5,
        )
        if payload.get("status") == "done":
            return payload
        if payload.get("status") == "error":
            raise AssertionError(payload.get("error"))
        time.sleep(1)
    raise TimeoutError(f"request {request_id} did not finish within {timeout}s")


@unittest.skipUnless(
    RUN_FULL_LOCAL,
    "set VENTIS_RUN_FULL_LOCAL=1 to run the full local build/deploy smoke test",
)
class FullLocalDeployTests(unittest.TestCase):
    """Build, deploy, and exercise a local-only generated Ventis project."""

    def setUp(self):
        if not _docker_available():
            raise unittest.SkipTest("Docker daemon is not available")
        self.tmpdir = tempfile.mkdtemp(prefix="ventis_full_local_")
        self.project_name = "local_smoke"
        self.project_dir = os.path.join(self.tmpdir, self.project_name)
        self.deploy = None

    def tearDown(self):
        if self.deploy and self.deploy.poll() is None:
            self.deploy.send_signal(signal.SIGTERM)
            try:
                self.deploy.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.deploy.kill()
                self.deploy.wait(timeout=10)
        if self.deploy and self.deploy.stdout:
            self.deploy.stdout.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generated_local_project_builds_deploys_and_routes(self):
        result = _run_ventis(["new-project", self.project_name], cwd=self.tmpdir)
        self.assertEqual(result.returncode, 0, result.stderr)

        self._force_local_only_config()

        result = _run_ventis(["build"], cwd=self.project_dir, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.deploy = subprocess.Popen(
            [sys.executable, "-m", "ventis.cli", "deploy"],
            cwd=self.project_dir,
            env={
                **os.environ,
                "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        _wait_for_http("http://localhost:8080/main", timeout=90)
        self._assert_routing_metadata()

        status, submitted = _request_json(
            "POST",
            "http://localhost:8080/main",
            {"name": "LocalSmoke"},
            timeout=5,
        )
        self.assertEqual(status, 202)

        completed = _wait_for_done(submitted["request_id"], timeout=90)
        self.assertEqual(completed["status"], "done")
        self.assertEqual(
            completed["result"],
            {"greeting": "Hello, LocalSmoke! I'm the ExampleAgent."},
        )

    def _force_local_only_config(self):
        config_path = os.path.join(self.project_dir, "config", "global_controller.yaml")
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        for agent in config["agents"]:
            agent["provider"] = "local"
            agent["replicas"] = 1
            agent.setdefault("resources", {}).pop("gpu", None)

        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)

    def _assert_routing_metadata(self):
        redis = RedisClient(host="localhost", port=6379)
        services = redis.smembers("routing_table:services")
        self.assertIn("ExampleAgent", services)
        self.assertIn("Workflow", services)

        raw_endpoints = redis.hget("routing_table:endpoints", "ExampleAgent")
        self.assertIsNotNone(raw_endpoints)
        endpoints = json.loads(raw_endpoints)
        self.assertEqual(len(endpoints), 1)
        self.assertTrue(endpoints[0].startswith("host.docker.internal:"))


if __name__ == "__main__":
    unittest.main()
