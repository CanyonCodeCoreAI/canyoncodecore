import base64
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.cloud_provider_logic.EC2 import _runtime as ec2_runtime


class _FakeWaiter:
    def __init__(self):
        self.calls = []

    def wait(self, InstanceIds):
        self.calls.append(list(InstanceIds))


class _FakeEC2Client:
    def __init__(self, public_ip="54.10.20.30", private_ip="10.0.0.30"):
        self.public_ip = public_ip
        self.private_ip = private_ip
        self.instances = {}
        self.run_requests = []
        self.terminate_requests = []
        self.waiter = _FakeWaiter()

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
        return {
            "Reservations": [
                {"Instances": [self.instances[instance_id]]}
                for instance_id in InstanceIds
                if instance_id in self.instances
            ]
        }

    def terminate_instances(self, InstanceIds):
        self.terminate_requests.append(list(InstanceIds))
        return {}


class EC2RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_controller = ec2_runtime._controller
        self.original_key_path = ec2_runtime.DEFAULT_SSH_KEY_PATH
        self.fake_client = _FakeEC2Client()
        self.client_calls = []
        self.key_dir = tempfile.mkdtemp()
        self.private_key = os.path.join(self.key_dir, "ventis_ec2")
        self.public_key = self.private_key + ".pub"
        with open(self.private_key, "w") as f:
            f.write("PRIVATE")
        with open(self.public_key, "w") as f:
            f.write("ssh-ed25519 AAAA test@ventis\n")
        ec2_runtime.DEFAULT_SSH_KEY_PATH = self.private_key
        self.controller = SimpleNamespace(
            config={
                "redis": {"host": "redis.internal", "port": 6379},
                "ec2": {
                    "ami_id": "ami-123456",
                    "subnet_id": "subnet-123456",
                    "security_group_ids": ["sg-123456"],
                    "region": "us-east-1",
                    "ssh_user": "ubuntu",
                    "public_ip_timeout": 1,
                },
            },
            registry_url=None,
            _run_cmd=MagicMock(
                return_value=SimpleNamespace(returncode=0, stderr="", stdout="")
            ),
        )
        ec2_runtime._controller = self.controller
        self.client_patch = patch.object(
            ec2_runtime.boto3,
            "client",
            side_effect=self._make_client,
        )
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        ec2_runtime._controller = self.original_controller
        ec2_runtime.DEFAULT_SSH_KEY_PATH = self.original_key_path

    def _make_client(self, service_name, region_name=None):
        self.client_calls.append(
            {"service_name": service_name, "region_name": region_name}
        )
        assert service_name == "ec2"
        return self.fake_client

    def test_aws_clients_fails_when_required_fields_are_missing(self):
        self.controller.config["ec2"].pop("ami_id")

        with self.assertRaisesRegex(ValueError, "Missing EC2 config"):
            ec2_runtime._aws_clients()

    def test_provision_uses_userdata_and_ec2_client(self):
        spec = {
            "name": "Tagged",
            "provider": "EC2",
            "instance_type": "t3.small",
            "redis_port": 6390,
        }

        provisioned = ec2_runtime.provision_instance(spec, 2)

        request = self.fake_client.run_requests[0]
        self.assertEqual(request["ImageId"], "ami-123456")
        self.assertNotIn("KeyName", request)
        self.assertIn("UserData", request)
        userdata = base64.b64decode(request["UserData"]).decode()
        self.assertIn("ssh-ed25519 AAAA test@ventis", userdata)
        self.assertIn("/home/ubuntu/.ssh", userdata)
        self.assertEqual(
            request["TagSpecifications"][0]["Tags"][0],
            {"Key": "Name", "Value": "ventis-Tagged-2"},
        )
        self.assertEqual(self.fake_client.waiter.calls, [["i-test1"]])
        self.assertEqual(provisioned["host"], "10.0.0.30")
        self.assertEqual(
            self.client_calls,
            [{"service_name": "ec2", "region_name": "us-east-1"}],
        )

    def test_provision_and_bootstrap_instance_return_runtime_record(self):
        spec = {
            "name": "Tagged",
            "provider": "EC2",
            "instance_type": "t3.small",
            "redis_port": 6390,
        }

        with (
            patch.object(ec2_runtime, "_bootstrap_instance"),
            patch.object(ec2_runtime, "_check_controller_health", return_value=True),
        ):
            provisioned = ec2_runtime.provision_instance(spec, 2)
            instance = ec2_runtime.bootstrap_instance(provisioned, spec, 2)

        self.assertEqual(instance["host"], "10.0.0.30")
        self.assertEqual(instance["endpoint"], "10.0.0.30:50051")
        self.assertEqual(instance["redis_host"], "10.0.0.30")
        self.assertEqual(instance["redis_port"], "6390")
        self.assertIn("--i-test1", instance["runtime_id"])

    def test_bootstrap_instance_terminates_instance_when_bootstrap_fails(self):
        spec = {"name": "Broken", "provider": "EC2", "instance_type": "t3.small"}
        provisioned = ec2_runtime.provision_instance(spec, 0)

        with (
            patch.object(
                ec2_runtime, "_bootstrap_instance", side_effect=RuntimeError("boom")
            ),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            ec2_runtime.bootstrap_instance(provisioned, spec, 0)

        self.assertEqual(self.fake_client.terminate_requests, [["i-test1"]])

    def test_bootstrap_uses_ssh_user(self):
        spec = {"name": "Tagged", "provider": "EC2", "redis_port": 6390}
        with (
            patch.object(ec2_runtime.time, "sleep"),
            patch.object(
                ec2_runtime.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stderr="", stdout=""),
            ),
        ):
            ec2_runtime._bootstrap_instance(
                "10.0.0.30",
                spec,
                2,
                self.controller.config["ec2"],
                redis_host="10.0.0.30",
                redis_port=6390,
            )

        self.assertTrue(self.controller._run_cmd.called)
        for call in self.controller._run_cmd.call_args_list:
            self.assertEqual(call.kwargs["user"], "ubuntu")
        # SSH wait + Redis container + agent container
        self.assertEqual(self.controller._run_cmd.call_count, 3)
        self.assertEqual(
            self.controller._run_cmd.call_args_list[-1].args[0][0], "docker"
        )

    def test_bootstrap_instance_terminates_instance_when_health_check_fails(self):
        spec = {"name": "Broken", "provider": "EC2", "instance_type": "t3.small"}
        provisioned = ec2_runtime.provision_instance(spec, 0)

        with (
            patch.object(ec2_runtime, "_bootstrap_instance"),
            patch.object(
                ec2_runtime,
                "_check_controller_health",
                side_effect=TimeoutError("boom"),
            ),
            self.assertRaises(TimeoutError),
        ):
            ec2_runtime.bootstrap_instance(provisioned, spec, 0)

        self.assertEqual(self.fake_client.terminate_requests, [["i-test1"]])

    def test_terminate_instance_still_cleans_host_side_maps(self):
        spec = {"name": "Tagged", "provider": "EC2", "instance_type": "t3.small"}
        provisioned = ec2_runtime.provision_instance(spec, 0)
        self.controller.redis_containers = {"10.0.0.30": "redis-box"}
        self.controller.node_redis = {"10.0.0.30": object()}

        ec2_runtime.terminate_instance(
            {
                "runtime_id": provisioned["runtime_id"],
                "host": "10.0.0.30",
            }
        )

        self.assertEqual(self.fake_client.terminate_requests, [["i-test1"]])
        self.assertNotIn("10.0.0.30", self.controller.redis_containers)
        self.assertNotIn("10.0.0.30", self.controller.node_redis)

    def test_health_check_error_message_mentions_ec2_runtime_endpoint(self):
        with patch.object(ec2_runtime.time, "time", side_effect=[0, 999]):
            with self.assertRaisesRegex(
                TimeoutError, "EC2 runtime endpoint never became reachable"
            ):
                ec2_runtime._check_controller_health("10.0.0.30:50051", timeout=1)


if __name__ == "__main__":
    unittest.main()
