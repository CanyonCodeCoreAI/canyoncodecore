import json
import sys
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

local_controler_pb2 = ModuleType("local_controler_pb2")


class _JsonResponse:
    def __init__(self, resonse):
        self.resonse = resonse


local_controler_pb2.JsonResponse = _JsonResponse
sys.modules.setdefault("local_controler_pb2", local_controler_pb2)

local_controler_pb2_grpc = ModuleType("local_controler_pb2_grpc")
local_controler_pb2_grpc.LocalControllerStub = type("LocalControllerStub", (), {})
local_controler_pb2_grpc.__file__ = "local_controler_pb2_grpc.py"
sys.modules.setdefault("local_controler_pb2_grpc", local_controler_pb2_grpc)
sys.modules.setdefault("grpc", ModuleType("grpc"))

from ventis.controller.global_controller import GlobalController
from ventis.controller.runtime_manager import RuntimeManager
from ventis.controller.cloud_provider_logic.EC2 import _runtime as ec2_runtime_impl


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


class FakeRedis:
    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}
        self.client = self

    def set(self, key, value):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            self.sets.pop(key, None)

    def hset(self, name, field, value):
        self.hashes.setdefault(name, {})[field] = value

    def hset_multiple(self, name, mapping):
        self.hashes.setdefault(name, {}).update(mapping)

    def hget(self, name, field):
        return self.hashes.get(name, {}).get(field)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hdel(self, name, field):
        self.hashes.setdefault(name, {}).pop(field, None)

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)

    def srem(self, name, *values):
        members = self.sets.setdefault(name, set())
        members.difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(name, set()))

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        return [key for key in sorted(keys) if key.startswith(prefix)]


class FakeController:
    def __init__(self):
        self.redis = FakeRedis()
        self.node_redis = {}
        self.redis_containers = {}
        self.containers = {}
        self.runtime_ids = set()
        self.run_calls = []
        self.shipped_images = []
        self.synced_projects = []
        self.config = {
            "redis": {"host": "redis.internal", "port": 6379},
            "ec2": {
                "ami_id": "ami-123456",
                "instance_type": "t3.small",
                "subnet_id": "subnet-123456",
                "security_group_ids": ["sg-123456"],
                "ssh_user": "ubuntu",
                "region": "us-east-1",
                "key_name": "ventis-key",
                "controller_health_timeout": 1,
                "public_ip_timeout": 1,
            },
        }

    def _run_cmd(self, cmd, host, user=None):
        self.run_calls.append((cmd, host, user))
        if cmd[:2] == ["docker", "inspect"]:
            runtime_id = cmd[2]
            return SimpleNamespace(
                returncode=0 if runtime_id in self.runtime_ids else 1,
                stdout="",
                stderr="missing",
            )
        if cmd[:3] == ["docker", "rm", "-f"]:
            self.runtime_ids.discard(cmd[3])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["docker", "run"]:
            runtime_id = cmd[cmd.index("--name") + 1]
            self.runtime_ids.add(runtime_id)
            return SimpleNamespace(returncode=0, stdout=f"{runtime_id}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _ensure_image_on_host(self, image, host, user):
        if _is_local_host(host):
            return
        self.shipped_images.append((image, host, user))

    def _sync_project_to_host(self, host, user, remote_dir):
        if _is_local_host(host):
            return
        self.synced_projects.append((host, user, remote_dir))

    def _ensure_remote_docker(self, host, user=None):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def ensure_host_redis(self, host, user=None, redis_port=6379, ssh_host=None):
        if host not in self.node_redis:
            self._run_cmd(
                [
                    "docker", "run", "-d",
                    "--name", f"ventis-redis-{host.replace('.', '-')}",
                    "-p", f"{redis_port}:6379",
                    "redis:alpine",
                ],
                ssh_host or host,
                user,
            )
            self.redis_containers[host] = f"ventis-redis-{host.replace('.', '-')}"
            self.node_redis[host] = FakeRedis()
        return self.node_redis[host]


class FakeWaiter:
    def __init__(self):
        self.calls = []

    def wait(self, InstanceIds):
        self.calls.append(list(InstanceIds))


class FakeEC2Client:
    def __init__(self, public_ip="54.10.20.30", private_ip="10.0.0.30"):
        self.public_ip = public_ip
        self.private_ip = private_ip
        self.run_requests = []
        self.terminate_requests = []
        self.waiter = FakeWaiter()
        self.instances = {}

    def run_instances(self, **kwargs):
        self.run_requests.append(kwargs)
        instance_id = f"i-test{len(self.run_requests)}"
        self.instances[instance_id] = {
            "InstanceId": instance_id,
            "State": {"Name": "running"},
            "PrivateIpAddress": self.private_ip,
            "PublicIpAddress": self.public_ip,
        }
        return {"Instances": [{"InstanceId": instance_id}]}

    def get_waiter(self, name):
        assert name == "instance_running"
        return self.waiter

    def describe_instances(self, InstanceIds):
        reservations = []
        for instance_id in InstanceIds:
            if instance_id in self.instances:
                reservations.append({"Instances": [self.instances[instance_id]]})
        return {"Reservations": reservations}

    def terminate_instances(self, InstanceIds):
        self.terminate_requests.append(list(InstanceIds))
        return {}


class FakeSession:
    def __init__(self, client, region_name="us-east-1", credentials=True):
        self._client = client
        self.region_name = region_name
        self._credentials = object() if credentials else None
        self.client_calls = []

    def get_credentials(self):
        return self._credentials

    def client(self, service_name, region_name=None):
        self.client_calls.append((service_name, region_name))
        return self._client


class RuntimeManagerTests(unittest.TestCase):
    def setUp(self):
        self.fake_ec2_client = FakeEC2Client()
        self.fake_ec2_session = FakeSession(self.fake_ec2_client)
        self.ec2_session_patch = patch.object(
            ec2_runtime_impl.boto3,
            "Session",
            side_effect=lambda **_kwargs: self.fake_ec2_session,
        )
        self.health_patch = patch.object(ec2_runtime_impl, "_check_controller_health", return_value=True)
        self.ec2_session_patch.start()
        self.health_patch.start()

    def tearDown(self):
        self.health_patch.stop()
        self.ec2_session_patch.stop()

    def test_ensure_instances_creates_missing_instances_and_writes_redis_records(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Alpha",
                    "provider": "local",
                    "replicas": 2,
                    "redis_port": 6379,
                }
            ]
        )

        self.assertEqual(
            controller.redis.smembers("agent:Alpha:instances"),
            {"local:Alpha:0", "local:Alpha:1"},
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:local:Alpha:0"),
            {
                "agent_name": "Alpha",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8000",
                "container_port": "50051",
                "endpoint": "localhost:8000",
                "redis_host": "host.docker.internal",
                "redis_port": "6379",
                "runtime_id": "ventis-local-alpha-0",
            },
        )
        self.assertEqual(
            json.loads(controller.redis.hget("routing_table:endpoints", "Alpha")),
            ["host.docker.internal:8000", "host.docker.internal:8001"],
        )
        self.assertEqual(controller.redis.hget("routing_table:stateful", "Alpha"), None)
        self.assertEqual(controller.redis.smembers("routing_table:services"), {"Alpha"})

    def test_stable_instance_ids_include_provider(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Beta",
                    "provider": "EC2",
                    "replicas": 1,
                    "redis_port": 6380,
                }
            ]
        )

        self.assertIn("EC2:Beta:0", controller.redis.smembers("agent:Beta:instances"))
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Beta:0")["provider"],
            "EC2",
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Beta:0")["host_port"],
            "50051",
        )

    def test_stateful_and_services_metadata_are_synced(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.sync_routing_metadata(
            [
                {"name": "Sticky", "stateful": True},
                {"name": "Plain", "stateful": False},
            ]
        )

        self.assertEqual(controller.redis.hget("routing_table:stateful", "Sticky"), "true")
        self.assertEqual(controller.redis.hget("routing_table:stateful", "Plain"), None)
        self.assertEqual(
            controller.redis.smembers("routing_table:services"),
            {"Sticky", "Plain"},
        )

    def test_sync_routing_metadata_removes_stale_service_and_stateful_flags(self):
        controller = FakeController()
        redis = controller.redis
        manager = RuntimeManager(controller, redis)

        redis.sadd("routing_table:services", "Old", "Keep")
        redis.hset("routing_table:stateful", "Old", "true")
        redis.hset("routing_table:stateful", "Keep", "true")

        manager.sync_routing_metadata([{"name": "Keep", "stateful": False}])

        self.assertEqual(redis.smembers("routing_table:services"), {"Keep"})
        self.assertEqual(redis.hget("routing_table:stateful", "Old"), None)
        self.assertEqual(redis.hget("routing_table:stateful", "Keep"), None)

    def test_stale_redis_instance_gets_recreated_when_runtime_is_missing(self):
        controller = FakeController()
        redis = controller.redis
        manager = RuntimeManager(controller, redis)

        redis.hset_multiple(
            "agent_instance:local:Gamma:0",
            {
                "agent_name": "Gamma",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8000",
                "container_port": "50051",
                "endpoint": "localhost:8000",
                "redis_host": "host.docker.internal",
                "redis_port": "6379",
                "runtime_id": "stale-runtime",
            },
        )
        redis.sadd("agent:Gamma:instances", "local:Gamma:0")

        manager.ensure_instances(
            [
                {
                    "name": "Gamma",
                    "provider": "local",
                    "replicas": 1,
                    "redis_port": 6379,
                }
            ]
        )

        self.assertEqual(
            redis.hgetall("agent_instance:local:Gamma:0")["runtime_id"],
            "ventis-local-gamma-0",
        )
        self.assertTrue(any(call[0][:2] == ["docker", "run"] for call in controller.run_calls))

    def test_existing_runtime_is_reused_without_relaunch(self):
        controller = FakeController()
        redis = controller.redis
        controller.runtime_ids.add("ventis-local-epsilon-0")
        manager = RuntimeManager(controller, redis)

        redis.hset_multiple(
            "agent_instance:local:Epsilon:0",
            {
                "agent_name": "Epsilon",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8000",
                "container_port": "50051",
                "endpoint": "localhost:8000",
                "redis_host": "host.docker.internal",
                "redis_port": "6379",
                "runtime_id": "ventis-local-epsilon-0",
            },
        )
        redis.sadd("agent:Epsilon:instances", "local:Epsilon:0")

        manager.ensure_instances(
            [
                {
                    "name": "Epsilon",
                    "provider": "local",
                    "replicas": 1,
                    "redis_port": 6379,
                }
            ]
        )

        self.assertFalse(any(call[0][:2] == ["docker", "run"] for call in controller.run_calls))
        self.assertEqual(
            json.loads(redis.hget("routing_table:endpoints", "Epsilon")),
            ["host.docker.internal:8000"],
        )

    def test_local_provider_allocates_ports_without_yaml_port(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Zeta",
                    "provider": "local",
                    "replicas": 2,
                    "redis_port": 6379,
                }
            ]
        )

        self.assertEqual(
            json.loads(controller.redis.hget("routing_table:endpoints", "Zeta")),
            ["host.docker.internal:8000", "host.docker.internal:8001"],
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:local:Zeta:0")["host_port"],
            "8000",
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:local:Zeta:1")["host_port"],
            "8001",
        )

    def test_ec2_launch_includes_redis_and_agent_env(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Delta",
                    "provider": "EC2",
                    "replicas": 1,
                    "redis_port": 6380,
                }
            ]
        )

        docker_run, host, user = next(
            (cmd, host, user)
            for cmd, host, user in controller.run_calls
            if cmd[:2] == ["docker", "run"] and "ventis-delta" in cmd
        )
        self.assertEqual(host, "54.10.20.30")
        self.assertEqual(user, "ubuntu")
        self.assertIn("VENTIS_REDIS_HOST=10.0.0.30", docker_run)
        self.assertIn("VENTIS_REDIS_PORT=6380", docker_run)
        self.assertIn("VENTIS_AGENT_HOST=10.0.0.30", docker_run)
        self.assertIn("VENTIS_AGENT_PORT=50051", docker_run)
        self.assertEqual(controller.shipped_images, [("ventis-delta", "54.10.20.30", "ubuntu")])
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Delta:0")["endpoint"],
            "10.0.0.30:50051",
        )
        self.assertIn("10.0.0.30", controller.node_redis)
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Delta:0")["redis_host"],
            "10.0.0.30",
        )

    def test_ec2_instance_can_be_created_and_removed(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Destroyable",
                    "provider": "EC2",
                    "replicas": 1,
                    "redis_port": 6380,
                }
            ]
        )

        self.assertIn(
            "EC2:Destroyable:0",
            controller.redis.smembers("agent:Destroyable:instances"),
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Destroyable:0")["endpoint"],
            "10.0.0.30:50051",
        )

        manager.remove_instance("EC2:Destroyable:0")

        self.assertEqual(self.fake_ec2_client.terminate_requests, [["i-test1"]])
        self.assertEqual(
            controller.redis.smembers("agent:Destroyable:instances"),
            set(),
        )
        self.assertEqual(
            controller.redis.hgetall("agent_instance:EC2:Destroyable:0"),
            {},
        )

    def test_ec2_agent_entrypoint_uses_generic_image_and_bind_mount(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Mounted",
                    "provider": "EC2",
                    "replicas": 1,
                    "redis_port": 6380,
                    "entrypoint": "agents/mounted_agent.py",
                }
            ]
        )

        docker_run, host, user = next(
            (cmd, host, user)
            for cmd, host, user in controller.run_calls
            if cmd[:2] == ["docker", "run"] and "ventis-ec2-mounted-0" in cmd
        )
        self.assertEqual((host, user), ("54.10.20.30", "ubuntu"))
        self.assertIn("ventis-agent-base", docker_run)
        self.assertIn("/opt/ventis/project:/workspace", docker_run)
        self.assertIn("VENTIS_AGENT_NAME=Mounted", docker_run)
        self.assertIn("VENTIS_AGENT_FILE=agents/mounted_agent.py", docker_run)
        self.assertEqual(
            controller.synced_projects,
            [("54.10.20.30", "ubuntu", "/opt/ventis/project")],
        )
        self.assertEqual(
            controller.shipped_images,
            [("ventis-agent-base", "54.10.20.30", "ubuntu")],
        )

    def test_mixed_local_and_ec2_publish_routing_snapshot_to_host_redis(self):
        controller = FakeController()
        controller.node_redis["localhost"] = FakeRedis()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "LocalA",
                    "provider": "local",
                    "replicas": 1,
                    "redis_port": 6379,
                    "stateful": True,
                },
                {
                    "name": "RemoteB",
                    "provider": "EC2",
                    "replicas": 1,
                    "redis_port": 6380,
                },
            ]
        )
        manager.publish_policy_rules([{"match": {"role": "admin"}, "access": "all"}])

        self.assertIn("local:LocalA:0", controller.redis.smembers("agent:LocalA:instances"))
        self.assertIn("EC2:RemoteB:0", controller.redis.smembers("agent:RemoteB:instances"))
        self.assertIsNone(controller.redis.hget("routing_table:endpoints", "LocalA"))

        for host in ("localhost", "10.0.0.30"):
            host_redis = controller.node_redis[host]
            self.assertEqual(
                host_redis.smembers("routing_table:services"),
                {"LocalA", "RemoteB"},
            )
            self.assertEqual(host_redis.hget("routing_table:stateful", "LocalA"), "true")
            self.assertEqual(
                json.loads(host_redis.hget("routing_table:endpoints", "LocalA")),
                ["host.docker.internal:8000"],
            )
            self.assertEqual(
                json.loads(host_redis.hget("routing_table:endpoints", "RemoteB")),
                ["10.0.0.30:50051"],
            )
            self.assertEqual(
                json.loads(host_redis.get("policy:rules")),
                [{"match": {"role": "admin"}, "access": "all"}],
            )

    def test_ec2_validate_config_fails_when_required_fields_are_missing(self):
        controller = FakeController()
        controller.config["ec2"].pop("ami_id")
        ec2_runtime_impl._set_controller(controller)

        with self.assertRaisesRegex(ValueError, "Missing EC2 config"):
            ec2_runtime_impl.validate_config()

    def test_ec2_create_instance_tags_waits_and_returns_runtime_id_with_instance_id(self):
        controller = FakeController()
        ec2_runtime_impl._set_controller(controller)

        instance = ec2_runtime_impl.create_instance(
            {"name": "Tagged", "provider": "EC2", "redis_port": 6390},
            2,
        )

        request = self.fake_ec2_client.run_requests[0]
        self.assertEqual(request["ImageId"], "ami-123456")
        self.assertEqual(request["KeyName"], "ventis-key")
        tags = request["TagSpecifications"][0]["Tags"]
        self.assertIn({"Key": "Name", "Value": "ventis-Tagged-2"}, tags)
        self.assertIn({"Key": "VentisManaged", "Value": "true"}, tags)
        self.assertIn({"Key": "VentisReplica", "Value": "2"}, tags)
        self.assertEqual(self.fake_ec2_client.waiter.calls, [["i-test1"]])
        self.assertEqual(instance["host"], "10.0.0.30")
        self.assertEqual(instance["endpoint"], "10.0.0.30:50051")
        self.assertIn("--i-test1", instance["runtime_id"])

    def test_ec2_create_instance_terminates_when_health_check_fails(self):
        controller = FakeController()
        ec2_runtime_impl._set_controller(controller)

        with patch.object(ec2_runtime_impl, "_check_controller_health", side_effect=TimeoutError("boom")):
            with self.assertRaises(TimeoutError):
                ec2_runtime_impl.create_instance({"name": "Broken", "provider": "EC2"}, 0)

        self.assertEqual(self.fake_ec2_client.terminate_requests, [["i-test1"]])

    def test_workflow_launch_uses_runtime_managed_api_port(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Workflow",
                    "provider": "local",
                    "type": "workflow",
                    "replicas": 1,
                    "redis_port": 6379,
                }
            ]
        )

        docker_run = next(
            cmd for cmd, _host, _user in controller.run_calls if cmd[:2] == ["docker", "run"]
        )
        self.assertIn("8000:50051", docker_run)
        self.assertIn("8080:8080", docker_run)

    def test_list_instances_returns_all_records_sorted_by_key(self):
        controller = FakeController()
        redis = controller.redis
        manager = RuntimeManager(controller, redis)
        redis.hset_multiple(
            "agent_instance:local:Beta:0",
            {
                "agent_name": "Beta",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8001",
                "endpoint": "localhost:8001",
                "runtime_id": "ventis-local-beta-0",
            },
        )
        redis.hset_multiple(
            "agent_instance:local:Alpha:0",
            {
                "agent_name": "Alpha",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8000",
                "endpoint": "localhost:8000",
                "runtime_id": "ventis-local-alpha-0",
            },
        )

        self.assertEqual(
            [instance["agent_name"] for instance in manager.list_instances()],
            ["Alpha", "Beta"],
        )

    def test_list_instances_for_agent_ignores_missing_records(self):
        controller = FakeController()
        redis = controller.redis
        manager = RuntimeManager(controller, redis)
        redis.sadd("agent:Alpha:instances", "local:Alpha:0", "local:Alpha:1")
        redis.hset_multiple(
            "agent_instance:local:Alpha:0",
            {
                "agent_name": "Alpha",
                "provider": "local",
                "replica_index": "0",
                "host": "localhost",
                "host_port": "8000",
                "endpoint": "localhost:8000",
                "runtime_id": "ventis-local-alpha-0",
            },
        )

        self.assertEqual(len(manager.list_instances("Alpha")), 1)

    def test_remove_missing_instance_is_noop(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        manager.remove_instance("local:Missing:0")

        self.assertEqual(controller.run_calls, [])

    def test_instance_ids_are_stable_and_provider_scoped(self):
        self.assertEqual(
            RuntimeManager._instance_id("local", "Alpha", 2),
            "local:Alpha:2",
        )
        self.assertEqual(
            RuntimeManager._instance_key("EC2", "Alpha", 2),
            "agent_instance:EC2:Alpha:2",
        )


class GlobalControllerRuntimeBackedTests(unittest.TestCase):
    def make_controller(self, instances):
        controller = GlobalController.__new__(GlobalController)
        controller.controllers = []
        controller.redis = FakeRedis()
        controller.node_redis = {}
        controller.redis_containers = {}
        controller._last_status = {}
        controller._lc_stubs = {}
        controller.containers = {}
        controller.runtime_manager = SimpleNamespace(
            list_instances=lambda agent_name=None: list(instances),
            _user_for_instance=lambda instance: "ubuntu",
        )
        controller._healthy_calls = []
        controller._unhealthy_calls = []
        controller._on_controller_healthy = (
            lambda name, host, port: controller._healthy_calls.append((name, host, port))
        )
        controller._on_controller_unhealthy = (
            lambda name, host, port: controller._unhealthy_calls.append((name, host, port))
        )
        controller._run_cmd_calls = []

        def _run_cmd(cmd, host, user=None):
            controller._run_cmd_calls.append((cmd, host, user))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_cmd = _run_cmd
        controller._ensure_remote_docker = (
            lambda host, user=None: SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        return controller

    def test_wait_for_healthy_uses_runtime_manager_instances(self):
        instance = {
            "agent_name": "Alpha",
            "host": "localhost",
            "host_port": "8000",
            "endpoint": "localhost:8000",
            "runtime_id": "ventis-local-alpha-0",
        }
        controller = self.make_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8000:status",
            "healthy",
        )

        GlobalController._wait_for_healthy(controller, timeout=1, interval=0)

        self.assertEqual(controller._last_status, {("localhost", "8000"): "healthy"})

    def test_poll_controllers_uses_runtime_manager_instances(self):
        instance = {
            "agent_name": "Beta",
            "host": "localhost",
            "host_port": "8000",
            "endpoint": "localhost:8000",
            "runtime_id": "ventis-local-beta-0",
        }
        controller = self.make_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8000:status",
            "healthy",
        )

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [("Beta", "localhost", "8000")])
        self.assertEqual(controller._unhealthy_calls, [])

    def test_trigger_cleanup_uses_runtime_manager_instances(self):
        instance = {
            "agent_name": "Gamma",
            "host": "localhost",
            "host_port": "9301",
            "endpoint": "localhost:9301",
            "runtime_id": "ventis-local-gamma-0",
        }
        controller = self.make_controller([instance])
        controller.redis.sadd("request:completed", "req-1")
        seen = []

        class Stub:
            def Cleanup(self, message):
                seen.append(json.loads(message.resonse))

        controller._get_lc_stub = lambda endpoint: Stub()

        GlobalController._trigger_cleanup(controller)

        self.assertEqual(seen, [{"request_id": "req-1"}])
        self.assertEqual(controller.redis.smembers("request:completed"), set())

    def test_stop_docker_agents_uses_runtime_manager_instances(self):
        instance = {
            "agent_name": "Delta",
            "host": "10.0.0.7",
            "host_port": "9401",
            "endpoint": "10.0.0.7:9401",
            "runtime_id": "ventis-ec2-delta-0",
            "provider": "EC2",
            "replica_index": "0",
        }
        controller = self.make_controller([instance])
        controller.containers = {"Delta": ["ventis-ec2-delta-0"]}
        removed = []
        controller.runtime_manager.remove_instance = lambda instance_id: removed.append(instance_id)
        controller.runtime_manager._instance_id_from_record = (
            lambda item: f"{item['provider']}:{item['agent_name']}:{item['replica_index']}"
        )

        GlobalController._stop_docker_agents(controller)

        self.assertEqual(removed, ["EC2:Delta:0"])
        self.assertEqual(controller._run_cmd_calls, [])
        self.assertEqual(controller.containers, {})

    def test_launch_redis_containers_keeps_central_redis(self):
        controller = self.make_controller([])
        central_redis = controller.redis
        controller.runtime_manager = SimpleNamespace(
            list_runtime_nodes=lambda agent_specs=None: {
                "localhost": {"user": None, "redis_port": 6379}
            }
        )

        with patch("ventis.controller.global_controller.RedisClient", side_effect=lambda **_kwargs: FakeRedis()):
            GlobalController._launch_redis_containers(controller)

        self.assertIs(controller.redis, central_redis)
        self.assertIn("localhost", controller.node_redis)
        self.assertIsNot(controller.node_redis["localhost"], central_redis)

    def test_ensure_host_redis_republishes_policy_rules_for_new_node(self):
        controller = self.make_controller([])
        published = []
        controller._load_policy_rules = lambda: [{"match": {"role": "admin"}, "access": "all"}]
        controller.runtime_manager = SimpleNamespace(
            list_runtime_nodes=lambda agent_specs=None: {},
            publish_policy_rules=lambda rules: published.append(rules),
        )

        with patch("ventis.controller.global_controller.RedisClient", side_effect=lambda **_kwargs: FakeRedis()):
            GlobalController.ensure_host_redis(controller, "54.10.20.30", "ubuntu", 6380)

        self.assertEqual(
            published,
            [[{"match": {"role": "admin"}, "access": "all"}]],
        )
        self.assertIn("54.10.20.30", controller.node_redis)

    def test_ensure_host_redis_waits_for_redis_before_registering_node(self):
        controller = self.make_controller([])
        waited = []
        controller._load_policy_rules = lambda: []
        controller.runtime_manager = SimpleNamespace(
            list_runtime_nodes=lambda agent_specs=None: {},
            publish_policy_rules=lambda rules: None,
        )

        with patch("ventis.controller.global_controller.RedisClient", side_effect=lambda **_kwargs: FakeRedis()):
            with patch.object(
                GlobalController,
                "_wait_for_redis",
                side_effect=lambda redis_client, host, port: waited.append((redis_client, host, port)),
            ):
                GlobalController.ensure_host_redis(controller, "54.10.20.30", "ubuntu", 6380)

        self.assertEqual(len(waited), 1)
        self.assertEqual(waited[0][1:], ("54.10.20.30", 6380))
        self.assertIs(controller.node_redis["54.10.20.30"], waited[0][0])

    def test_wait_for_redis_timeout_mentions_ec2_security_group(self):
        class FailingRedis:
            def set(self, *_args):
                raise TimeoutError("Timeout connecting to server")

        controller = self.make_controller([])

        with self.assertRaisesRegex(TimeoutError, "security group allows inbound TCP 6380"):
            GlobalController._wait_for_redis(
                controller,
                FailingRedis(),
                "54.10.20.30",
                6380,
                timeout=0,
                interval=0,
            )


if __name__ == "__main__":
    unittest.main()


class LegacyConfigRejectionTests(unittest.TestCase):
    def test_runtime_manager_rejects_legacy_host_port_yaml(self):
        controller = FakeController()
        manager = RuntimeManager(controller, controller.redis)

        with self.assertRaisesRegex(ValueError, "Legacy YAML host/port"):
            manager.ensure_instances([
                {
                    "name": "Legacy",
                    "provider": "local",
                    "host": "localhost",
                    "port": 9000,
                    "replicas": [{"host": "localhost", "port": 9000}],
                }
            ])
