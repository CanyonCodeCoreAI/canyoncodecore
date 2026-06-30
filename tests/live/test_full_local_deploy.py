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


def _docker_inspect_exists(name):
    result = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


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

    def _stop_deploy(self):
        if self.deploy and self.deploy.poll() is None:
            self.deploy.send_signal(signal.SIGTERM)
            self.deploy.wait(timeout=30)

    def _assert_runtime_containers_removed(self, runtime_ids, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if all(not _docker_inspect_exists(runtime_id) for runtime_id in runtime_ids):
                return
            time.sleep(1)
        still_present = [runtime_id for runtime_id in runtime_ids if _docker_inspect_exists(runtime_id)]
        self.fail(f"Runtime containers were not removed: {still_present}")

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

    def _force_two_example_agent_replicas(self):
        config_path = os.path.join(self.project_dir, "config", "global_controller.yaml")
        policy_path = os.path.join(self.project_dir, "config", "policy.yaml")

        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        with open(policy_path, "r") as f:
            policy = yaml.safe_load(f)

        config["agents"] = [
            agent for agent in config["agents"] if agent["name"] in {"ExampleAgent", "Workflow"}
        ]

        for agent in config["agents"]:
            agent["provider"] = "local"
            agent.setdefault("resources", {}).pop("gpu", None)
            if agent["name"] == "ExampleAgent":
                agent["replicas"] = 2
            elif agent["name"] == "Workflow":
                agent["replicas"] = 1

        policy.setdefault("autoscale", {}).setdefault("ExampleAgent", {})
        policy["autoscale"]["ExampleAgent"].update(
            {
                "queue_length_scale_up_threshold": 10,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 60,
                "max_replicas": 3,
            }
        )

        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)
        with open(policy_path, "w") as f:
            yaml.safe_dump(policy, f, sort_keys=False)

    def _assert_routing_metadata(self):
        redis = RedisClient(host="localhost", port=6379)
        deadline = time.time() + 10
        services = set()
        raw_endpoints = None
        raw_status = None
        while time.time() < deadline:
            services = redis.smembers("routing_table:services")
            raw_endpoints = redis.hget("routing_table:endpoints", "ExampleAgent")
            raw_status = redis.hget("routing_table:status", "ExampleAgent")
            if "ExampleAgent" in services and "Workflow" in services and raw_endpoints and raw_status:
                break
            time.sleep(0.5)

        self.assertIn("ExampleAgent", services)
        self.assertIn("Workflow", services)
        self.assertIsNotNone(raw_endpoints)
        self.assertIsNotNone(raw_status)
        endpoints = json.loads(raw_endpoints)
        statuses = json.loads(raw_status)
        self.assertEqual(len(endpoints), 1)
        self.assertTrue(endpoints[0].startswith("host.docker.internal:"))
        self.assertEqual(statuses[endpoints[0]], "Healthy")

    def _wait_for_example_agent_idle_and_scale_down(self):
        redis = RedisClient(host="localhost", port=6379)
        deadline = time.time() + 140
        started_at = time.time()
        seen_idle = None
        initial_endpoints = None

        while time.time() < deadline:
            raw_endpoints = redis.hget("routing_table:endpoints", "ExampleAgent")
            raw_status = redis.hget("routing_table:status", "ExampleAgent")
            if not raw_endpoints or not raw_status:
                time.sleep(1)
                continue

            endpoints = json.loads(raw_endpoints)
            statuses = json.loads(raw_status)
            if initial_endpoints is None and len(endpoints) == 2:
                initial_endpoints = list(endpoints)

            if seen_idle is None:
                idle_endpoints = [
                    endpoint
                    for endpoint, status in statuses.items()
                    if status == "Idling"
                ]
                if idle_endpoints:
                    seen_idle = (time.time(), idle_endpoints[0], list(endpoints))

            if seen_idle and initial_endpoints and len(endpoints) == 1:
                removed = [endpoint for endpoint in initial_endpoints if endpoint not in endpoints]
                self.assertEqual(len(removed), 1)
                return {
                    "idle_after_seconds": round(seen_idle[0] - started_at, 1),
                    "removed_endpoint": removed[0],
                    "remaining_endpoint": endpoints[0],
                }

            time.sleep(1)

        raise TimeoutError("ExampleAgent did not idle and scale down within the timeout")

    def test_example_agent_two_replicas_idle_then_scale_down(self):
        result = _run_ventis(["new-project", self.project_name], cwd=self.tmpdir)
        self.assertEqual(result.returncode, 0, result.stderr)

        self._force_two_example_agent_replicas()

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

        deadline = time.time() + 60
        while time.time() < deadline:
            redis = RedisClient(host="localhost", port=6379)
            raw_endpoints = redis.hget("routing_table:endpoints", "ExampleAgent")
            raw_status = redis.hget("routing_table:status", "ExampleAgent")
            if raw_endpoints and raw_status:
                endpoints = json.loads(raw_endpoints)
                statuses = json.loads(raw_status)
                if len(endpoints) == 2 and all(statuses.get(endpoint) == "Healthy" for endpoint in endpoints):
                    break
            time.sleep(1)
        else:
            self.fail("ExampleAgent routing metadata did not show two healthy replicas")

        verification = self._wait_for_example_agent_idle_and_scale_down()
        self.assertGreaterEqual(verification["idle_after_seconds"], 55)
        self.assertLessEqual(verification["idle_after_seconds"], 90)

        self._stop_deploy()
        self._assert_runtime_containers_removed(
            [
                "ventis-local-exampleagent-0",
                "ventis-local-exampleagent-1",
                "ventis-local-workflow-0",
            ]
        )

    def _configure_three_agent_chain(self):
        config_path = os.path.join(self.project_dir, "config", "global_controller.yaml")
        policy_path = os.path.join(self.project_dir, "config", "policy.yaml")
        workflow_path = os.path.join(self.project_dir, "workflows", "example_workflow.py")
        agents_dir = os.path.join(self.project_dir, "agents")

        with open(config_path, "w") as f:
            yaml.safe_dump(
                {
                    "agents": [
                        {
                            "name": "AlphaAgent",
                            "replicas": 1,
                            "redis_port": 6379,
                            "resources": {"cpu": 1, "memory": 512},
                            "entrypoint": "agents/alpha_agent.py",
                            "provider": "local",
                        },
                        {
                            "name": "BetaAgent",
                            "replicas": 1,
                            "redis_port": 6379,
                            "resources": {"cpu": 1, "memory": 512},
                            "entrypoint": "agents/beta_agent.py",
                            "provider": "local",
                        },
                        {
                            "name": "GammaAgent",
                            "replicas": 1,
                            "redis_port": 6379,
                            "resources": {"cpu": 1, "memory": 512},
                            "entrypoint": "agents/gamma_agent.py",
                            "provider": "local",
                        },
                        {
                            "name": "Workflow",
                            "replicas": 1,
                            "type": "workflow",
                            "redis_port": 6379,
                            "workflow_file": "workflows/example_workflow.py",
                            "provider": "local",
                        },
                    ],
                    "poll_interval": 5,
                    "redis": {"host": "localhost", "port": 6379, "db": 0},
                },
                f,
                sort_keys=False,
            )

        with open(policy_path, "w") as f:
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "match": {},
                            "access": ["AlphaAgent", "BetaAgent", "GammaAgent", "Workflow"],
                        }
                    ]
                },
                f,
                sort_keys=False,
            )

        with open(os.path.join(agents_dir, "alpha_agent.yaml"), "w") as f:
            f.write(
                """agent:
  name: AlphaAgent
  functions:
    - name: start
      arguments:
        - name: text
          type: str
      returns:
        type: str
"""
            )
        with open(os.path.join(agents_dir, "beta_agent.yaml"), "w") as f:
            f.write(
                """agent:
  name: BetaAgent
  functions:
    - name: step
      arguments:
        - name: text
          type: str
      returns:
        type: str
"""
            )
        with open(os.path.join(agents_dir, "gamma_agent.yaml"), "w") as f:
            f.write(
                """agent:
  name: GammaAgent
  functions:
    - name: finish
      arguments:
        - name: text
          type: str
      returns:
        type: str
"""
            )

        with open(os.path.join(agents_dir, "alpha_agent.py"), "w") as f:
            f.write(
                """import os\nimport sys\nsys.path.insert(0, os.path.dirname(__file__))\nfrom beta_agent_stub import BetaAgentStub\n\n\nclass AlphaAgent(object):\n    def __init__(self):\n        self.tools = [self.start]\n\n    def start(self, text: str) -> str:\n        return BetaAgentStub().step(text=f\"{text} -> alpha\").value()\n"""
            )
        with open(os.path.join(agents_dir, "beta_agent.py"), "w") as f:
            f.write(
                """import os\nimport sys\nsys.path.insert(0, os.path.dirname(__file__))\nfrom gamma_agent_stub import GammaAgentStub\n\n\nclass BetaAgent(object):\n    def __init__(self):\n        self.tools = [self.step]\n\n    def step(self, text: str) -> str:\n        return GammaAgentStub().finish(text=f\"{text} -> beta\").value()\n"""
            )
        with open(os.path.join(agents_dir, "gamma_agent.py"), "w") as f:
            f.write(
                """class GammaAgent(object):\n    def __init__(self):\n        self.tools = [self.finish]\n\n    def finish(self, text: str) -> str:\n        return f\"{text} -> gamma\"\n"""
            )

        with open(workflow_path, "w") as f:
            f.write(
                """import os\nimport sys\nsys.path.insert(0, os.path.dirname(__file__))\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'stubs'))\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'grpc_stubs'))\nfrom deploy import deploy\nfrom alpha_agent_stub import AlphaAgentStub\n\n\ndef main(text: str = 'start', name: str = None):\n    if name is not None:\n        text = name\n    return {'result': AlphaAgentStub().start(text=text).value()}\n\n\ndeploy(main, port=8080)\n"""
            )

    def test_three_agent_chain_creates_routes_and_deletes_all_instances(self):
        result = _run_ventis(["new-project", self.project_name], cwd=self.tmpdir)
        self.assertEqual(result.returncode, 0, result.stderr)

        self._configure_three_agent_chain()

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

        redis = RedisClient(host="localhost", port=6379)
        deadline = time.time() + 30
        while time.time() < deadline:
            services = redis.smembers("routing_table:services")
            if {"AlphaAgent", "BetaAgent", "GammaAgent", "Workflow"}.issubset(services):
                break
            time.sleep(1)
        else:
            self.fail(f"Expected 4 services, got {services}")

        status, submitted = _request_json(
            "POST",
            "http://localhost:8080/main",
            {"text": "start"},
            timeout=5,
        )
        self.assertEqual(status, 202)
        completed = _wait_for_done(submitted["request_id"], timeout=90)
        self.assertEqual(completed["status"], "done")
        self.assertEqual(completed["result"], {"result": "start -> alpha -> beta -> gamma"})

        self._stop_deploy()
        self._assert_runtime_containers_removed(
            [
                "ventis-local-alphaagent-0",
                "ventis-local-betaagent-0",
                "ventis-local-gammaagent-0",
                "ventis-local-workflow-0",
            ]
        )


if __name__ == "__main__":
    unittest.main()
