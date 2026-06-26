import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.support.runtime_fakes import FakeRedis
from ventis.controller.runtime_manager import RuntimeManager


RUN_LIVE_DOCKER = os.environ.get("VENTIS_RUN_LIVE_DOCKER") == "1"


class LiveDockerController:
    def __init__(self):
        self.redis = FakeRedis()
        self.node_redis = {}
        self.redis_containers = {}
        self.containers = {}
        self.config = {"redis": {"host": "localhost", "port": 6379}}

    def _run_cmd(self, cmd, host, user=None):
        if host not in ("localhost", "127.0.0.1"):
            raise AssertionError(f"live local test cannot run remote command on {host}")
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _ensure_image_on_host(self, image, host, user):
        return None


@unittest.skipUnless(
    RUN_LIVE_DOCKER,
    "set VENTIS_RUN_LIVE_DOCKER=1 to run live local Docker runtime tests",
)
class LocalDockerRuntimeLiveTests(unittest.TestCase):
    image = "ventis-liveprobe"

    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker CLI is not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile = os.path.join(tmpdir, "Dockerfile")
            with open(dockerfile, "w") as f:
                f.write(
                    textwrap.dedent(
                        """
                        FROM alpine:3.20
                        CMD ["sh", "-c", "sleep 600"]
                        """
                    ).strip()
                )
            result = subprocess.run(
                ["docker", "build", "-t", cls.image, tmpdir],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise unittest.SkipTest(f"failed to build live Docker image: {result.stderr}")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", "ventis-local-liveprobe-0"], capture_output=True, text=True)
        subprocess.run(["docker", "rmi", "-f", cls.image], capture_output=True, text=True)

    def test_local_runtime_launches_container_and_writes_routing_metadata(self):
        controller = LiveDockerController()
        manager = RuntimeManager(controller, controller.redis)
        self.addCleanup(lambda: manager.remove_instance("local:LiveProbe:0"))

        instances = manager.ensure_instances(
            [{"name": "LiveProbe", "provider": "local", "replicas": 1}]
        )

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["endpoint"], "localhost:8000")
        self.assertEqual(
            controller.redis.hgetall("agent_instance:local:LiveProbe:0")["runtime_id"],
            "ventis-local-liveprobe-0",
        )
        inspect_result = subprocess.run(
            ["docker", "inspect", "ventis-local-liveprobe-0"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(inspect_result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
