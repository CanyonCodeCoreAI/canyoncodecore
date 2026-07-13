import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.cloud_provider_logic.Local import _runtime as local_runtime
from ventis.controller.instance_manager import InstanceManager


class _FakeRedis:
    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}

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
        self.sets.setdefault(name, set()).difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(name, set()))

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        return [key for key in sorted(keys) if key.startswith(prefix)]


def _fake_controller():
    redis = _FakeRedis()
    return SimpleNamespace(
        redis=redis,
        containers={},
        node_redis={},
        redis_containers={},
        _run_cmd=MagicMock(return_value=SimpleNamespace(returncode=0)),
    )


def _fake_runtime(**kwargs):
    runtime = SimpleNamespace(
        validate_config=MagicMock(),
        provision_instance=MagicMock(return_value={}),
        bootstrap_instance=MagicMock(return_value={}),
        terminate_instance=MagicMock(),
        routing_endpoint_for=MagicMock(
            side_effect=lambda instance: instance["endpoint"]
        ),
        _controller=None,
    )
    for key, value in kwargs.items():
        setattr(runtime, key, value)
    return runtime


class InstanceManagerRuntimeTests(unittest.TestCase):
    def test_local_instances_keep_default_host_and_increment_host_ports(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)

        alpha = manager.ensure_instances([{"name": "Alpha", "provider": "local"}])[0]
        beta = manager.ensure_instances([{"name": "Beta", "provider": "local"}])[0]

        self.assertEqual(
            alpha,
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
        self.assertEqual(beta["host"], "localhost")
        self.assertEqual(beta["host_port"], "8001")
        self.assertEqual(
            controller._run_cmd.call_args_list[0].args,
            (
                [
                    "docker",
                    "run",
                    "-d",
                    "-it",
                    "--add-host=host.docker.internal:host-gateway",
                    "--name",
                    "ventis-local-alpha-0",
                    "-p",
                    "8000:50051",
                    "-e",
                    "VENTIS_AGENT_PORT=8000",
                    "-e",
                    "VENTIS_AGENT_HOST=host.docker.internal",
                    "-e",
                    "VENTIS_REDIS_HOST=host.docker.internal",
                    "-e",
                    "VENTIS_REDIS_PORT=6379",
                    "ventis-alpha",
                ],
                "localhost",
                None,
            ),
        )

    def test_local_workflow_and_resource_flags_stay_the_same(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)

        manager.ensure_instances(
            [
                {
                    "name": "Workflow",
                    "provider": "local",
                    "type": "workflow",
                    "resources": {"cpu": 2, "memory": 1024, "gpu": 1},
                }
            ]
        )

        self.assertEqual(
            controller._run_cmd.call_args.args,
            (
                [
                    "docker",
                    "run",
                    "-d",
                    "-it",
                    "--add-host=host.docker.internal:host-gateway",
                    "--name",
                    "ventis-local-workflow-0",
                    "-p",
                    "8000:50051",
                    "-e",
                    "VENTIS_AGENT_PORT=8000",
                    "-e",
                    "VENTIS_AGENT_HOST=host.docker.internal",
                    "-e",
                    "VENTIS_REDIS_HOST=host.docker.internal",
                    "-e",
                    "VENTIS_REDIS_PORT=6379",
                    "-p",
                    "8080:8080",
                    "--cpus",
                    "2",
                    "--memory",
                    "1024m",
                    "--gpus",
                    "1",
                    "ventis-workflow",
                ],
                "localhost",
                None,
            ),
        )

    def test_local_remove_instance_still_removes_the_same_container(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)
        manager.ensure_instances([{"name": "Alpha", "provider": "local"}])

        controller._run_cmd.reset_mock()
        manager.remove_instance("local:Alpha:0")

        self.assertEqual(
            controller._run_cmd.call_args.args,
            (["docker", "rm", "-f", "ventis-local-alpha-0"], "localhost", None),
        )
        self.assertEqual(controller.redis.hgetall("agent_instance:local:Alpha:0"), {})
        self.assertEqual(controller.containers["Alpha"], [])

    def test_manager_keeps_ec2_runtime_boundary_behavior(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)

        provisioned = {
            "host": "10.0.0.30",
            "runtime_id": "ventis-ec2-remote-0--i-test1",
            "ec2_instance_id": "i-test1",
            "redis_port": 6390,
        }
        instance = {
            "agent_name": "Remote",
            "provider": "EC2",
            "replica_index": "0",
            "host": "10.0.0.30",
            "host_port": "50051",
            "container_port": "50051",
            "endpoint": "10.0.0.30:50051",
            "redis_host": "10.0.0.30",
            "redis_port": "6390",
            "runtime_id": "ventis-ec2-remote-0--i-test1",
            "ec2_instance_id": "i-test1",
        }

        runtime = _fake_runtime(
            provision_instance=MagicMock(return_value=provisioned),
            bootstrap_instance=MagicMock(return_value=instance),
        )
        del runtime.validate_config

        with patch.object(manager, "_provider_runtime", return_value=runtime):
            created = manager.ensure_instances(
                [
                    {
                        "name": "Remote",
                        "provider": "EC2",
                        "instance_type": "t3.small",
                        "redis_port": 6390,
                    }
                ]
            )[0]
            controller.node_redis = {"10.0.0.30": _FakeRedis()}
            controller.redis_containers = {"10.0.0.30": "redis-box"}
            manager.remove_instance("EC2:Remote:0")

        runtime.provision_instance.assert_called_once_with(
            {
                "name": "Remote",
                "provider": "EC2",
                "instance_type": "t3.small",
                "redis_port": 6390,
            },
            0,
            manager._next_host_port,
        )
        runtime.bootstrap_instance.assert_called_once_with(
            provisioned,
            {
                "name": "Remote",
                "provider": "EC2",
                "instance_type": "t3.small",
                "redis_port": 6390,
            },
            0,
        )
        runtime.terminate_instance.assert_called_once_with(instance)
        self.assertEqual(created, instance)

    def test_manager_uses_same_runtime_contract_for_local_and_ec2(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)

        local_instance = {
            "agent_name": "Local",
            "provider": "local",
            "replica_index": "0",
            "host": "localhost",
            "host_port": "8000",
            "container_port": "50051",
            "endpoint": "localhost:8000",
            "redis_host": "host.docker.internal",
            "redis_port": "6379",
            "runtime_id": "ventis-local-local-0",
        }
        ec2_instance = {
            "agent_name": "Remote",
            "provider": "EC2",
            "replica_index": "0",
            "host": "10.0.0.30",
            "host_port": "50051",
            "container_port": "50051",
            "endpoint": "10.0.0.30:50051",
            "redis_host": "10.0.0.30",
            "redis_port": "6379",
            "runtime_id": "ventis-ec2-remote-0--i-test1",
            "ec2_instance_id": "i-test1",
        }

        local_runtime = _fake_runtime(
            bootstrap_instance=MagicMock(return_value=local_instance),
            routing_endpoint_for=MagicMock(return_value="host.docker.internal:8000"),
        )
        ec2_runtime = _fake_runtime(
            bootstrap_instance=MagicMock(return_value=ec2_instance),
            routing_endpoint_for=MagicMock(return_value="10.0.0.30:50051"),
        )
        del ec2_runtime.validate_config

        def runtime_for(provider):
            return ec2_runtime if provider == "EC2" else local_runtime

        with patch.object(manager, "_provider_runtime", side_effect=runtime_for):
            manager.ensure_instances(
                [
                    {"name": "Local", "provider": "local"},
                    {"name": "Remote", "provider": "EC2", "instance_type": "t3.small"},
                ]
            )

        local_runtime.validate_config.assert_called_once_with()
        local_runtime.provision_instance.assert_called_once_with(
            {"name": "Local", "provider": "local"}, 0, manager._next_host_port
        )
        local_runtime.bootstrap_instance.assert_called_once_with(
            {}, {"name": "Local", "provider": "local"}, 0
        )
        ec2_runtime.provision_instance.assert_called_once_with(
            {"name": "Remote", "provider": "EC2", "instance_type": "t3.small"},
            0,
            manager._next_host_port,
        )
        ec2_runtime.bootstrap_instance.assert_called_once_with(
            {}, {"name": "Remote", "provider": "EC2", "instance_type": "t3.small"}, 0
        )

    def test_local_provider_runtime_does_not_require_ec2_import(self):
        controller = _fake_controller()
        manager = InstanceManager(controller, controller.redis)

        runtime = manager._provider_runtime("local")

        self.assertIs(runtime, local_runtime)


if __name__ == "__main__":
    unittest.main()
