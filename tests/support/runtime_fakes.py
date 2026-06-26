import json
import sys
from types import ModuleType
from types import SimpleNamespace


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def _container_routing_host(host):
    return "host.docker.internal" if _is_local_host(host) else host


def install_grpc_stubs():
    local_controler_pb2 = ModuleType("local_controler_pb2")

    class JsonResponse:
        def __init__(self, resonse):
            self.resonse = resonse

    local_controler_pb2.JsonResponse = JsonResponse
    sys.modules.setdefault("local_controler_pb2", local_controler_pb2)

    local_controler_pb2_grpc = ModuleType("local_controler_pb2_grpc")
    local_controler_pb2_grpc.LocalControllerStub = type("LocalControllerStub", (), {})
    local_controler_pb2_grpc.__file__ = "local_controler_pb2_grpc.py"
    sys.modules.setdefault("local_controler_pb2_grpc", local_controler_pb2_grpc)
    sys.modules.setdefault("grpc", ModuleType("grpc"))


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
        if not _is_local_host(host):
            self.shipped_images.append((image, host, user))

    def _sync_project_to_host(self, host, user, remote_dir):
        if not _is_local_host(host):
            self.synced_projects.append((host, user, remote_dir))

    def ensure_host_redis(self, host, user=None, redis_port=6379):
        if host not in self.node_redis:
            self.redis_containers[host] = f"ventis-redis-{host.replace('.', '-')}"
            self.node_redis[host] = FakeRedis()
        return self.node_redis[host]

    def _ensure_remote_docker(self, host, user=None):
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def make_instance(agent_name, replica_index, host="localhost", host_port=8000, provider="local"):
    runtime_id = f"ventis-{provider.lower()}-{agent_name.lower()}-{replica_index}"
    routing_host = _container_routing_host(host)
    return {
        "agent_name": agent_name,
        "provider": provider,
        "replica_index": str(replica_index),
        "host": host,
        "host_port": str(host_port),
        "container_port": "50051",
        "endpoint": f"{host}:{host_port}",
        "redis_host": routing_host,
        "redis_port": "6379",
        "runtime_id": runtime_id,
    }


def make_global_controller(instances):
    install_grpc_stubs()

    from ventis.controller.global_controller import GlobalController

    controller = GlobalController.__new__(GlobalController)
    controller.controllers = []
    controller.redis = FakeRedis()
    controller.node_redis = {}
    controller.redis_containers = {}
    controller.containers = {}
    controller._last_status = {}
    controller._lc_stubs = {}
    controller._healthy_calls = []
    controller._unhealthy_calls = []
    controller._run_cmd_calls = []

    controller.runtime_manager = SimpleNamespace(
        list_instances=lambda agent_name=None: list(instances),
        list_runtime_nodes=lambda agent_specs=None: {},
        _user_for_instance=lambda instance: instance.get("user"),
        _instance_id_from_record=lambda instance: (
            f"{instance['provider']}:{instance['agent_name']}:{instance['replica_index']}"
        ),
    )
    controller._on_controller_healthy = (
        lambda name, host, port: controller._healthy_calls.append((name, host, port))
    )
    controller._on_controller_unhealthy = (
        lambda name, host, port: controller._unhealthy_calls.append((name, host, port))
    )
    controller._run_cmd = (
        lambda cmd, host, user=None: controller._run_cmd_calls.append((cmd, host, user))
        or SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    controller._ensure_remote_docker = (
        lambda host, user=None: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    return controller


def cleanup_payloads(stub_calls):
    return [json.loads(message.resonse) for message in stub_calls]
