import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeSSMClient:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [{"Status": "Success"}])
        self.send_requests = []
        self.get_requests = []

    def send_command(self, **kwargs):
        self.send_requests.append(kwargs)
        return {"Command": {"CommandId": "cmd-123"}}

    def get_command_invocation(self, **kwargs):
        self.get_requests.append(kwargs)
        index = min(len(self.get_requests) - 1, len(self.statuses) - 1)
        return self.statuses[index]


class _FakeSession:
    def __init__(
        self, ec2_client, ssm_client, region_name="us-east-1", credentials=True
    ):
        self._ec2_client = ec2_client
        self._ssm_client = ssm_client
        self.region_name = region_name
        self._credentials = object() if credentials else None
        self.client_calls = []

    def get_credentials(self):
        return self._credentials

    def client(self, service_name, region_name=None, endpoint_url=None):
        self.client_calls.append(
            {
                "service_name": service_name,
                "region_name": region_name,
                "endpoint_url": endpoint_url,
            }
        )
        if service_name == "ec2":
            return self._ec2_client
        if service_name == "ssm":
            return self._ssm_client
        raise AssertionError(service_name)


class EC2RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_controller = ec2_runtime._controller
        self.fake_client = _FakeEC2Client()
        self.fake_ssm_client = _FakeSSMClient()
        self.fake_session = _FakeSession(self.fake_client, self.fake_ssm_client)
        self.session_calls = []
        self.controller = SimpleNamespace(
            config={
                "redis": {"host": "redis.internal", "port": 6379},
                "ec2": {
                    "ami_id": "ami-123456",
                    "subnet_id": "subnet-123456",
                    "security_group_ids": ["sg-123456"],
                    "region": "us-east-1",
                    "key_name": "ventis-key",
                    "profile": "ventis-profile",
                    "aws_access_key_id": "AKIA_TEST",
                    "aws_secret_access_key": "secret",
                    "aws_session_token": "token",
                    "public_ip_timeout": 1,
                },
            },
            registry_url=None,
        )
        ec2_runtime._controller = self.controller
        self.session_patch = patch.object(
            ec2_runtime.boto3,
            "Session",
            side_effect=self._make_session,
        )
        self.session_patch.start()

    def tearDown(self):
        self.session_patch.stop()
        ec2_runtime._controller = self.original_controller

    def _make_session(self, **kwargs):
        self.session_calls.append(kwargs)
        return self.fake_session

    def test_validate_config_fails_when_required_fields_are_missing(self):
        self.controller.config["ec2"].pop("ami_id")

        with self.assertRaisesRegex(ValueError, "Missing EC2 config"):
            ec2_runtime.validate_config()

    def test_provision_uses_minimal_name_tag_and_configured_session(self):
        spec = {
            "name": "Tagged",
            "provider": "EC2",
            "instance_type": "t3.small",
            "redis_port": 6390,
        }

        provisioned = ec2_runtime.provision_instance(spec, 2)

        request = self.fake_client.run_requests[0]
        self.assertEqual(request["ImageId"], "ami-123456")
        self.assertEqual(request["KeyName"], "ventis-key")
        self.assertEqual(
            request["TagSpecifications"],
            [
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": "ventis-Tagged-2"}],
                }
            ],
        )
        self.assertEqual(self.fake_client.waiter.calls, [["i-test1"]])
        self.assertEqual(provisioned["host"], "10.0.0.30")
        self.assertEqual(
            self.fake_session.client_calls,
            [
                {
                    "service_name": "ec2",
                    "region_name": "us-east-1",
                    "endpoint_url": None,
                },
                {
                    "service_name": "ssm",
                    "region_name": "us-east-1",
                    "endpoint_url": None,
                },
            ],
        )
        self.assertEqual(
            self.session_calls,
            [
                {
                    "region_name": "us-east-1",
                    "profile_name": "ventis-profile",
                    "aws_access_key_id": "AKIA_TEST",
                    "aws_secret_access_key": "secret",
                    "aws_session_token": "token",
                }
            ],
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
        self.assertEqual(self.session_calls[-1], self.session_calls[0])

    def test_validate_config_passes_endpoint_url_to_boto_clients(self):
        self.controller.config["ec2"]["endpoint_url"] = "http://localhost:4566"

        ec2_runtime.validate_config()

        self.assertEqual(
            self.fake_session.client_calls,
            [
                {
                    "service_name": "ec2",
                    "region_name": "us-east-1",
                    "endpoint_url": "http://localhost:4566",
                },
                {
                    "service_name": "ssm",
                    "region_name": "us-east-1",
                    "endpoint_url": "http://localhost:4566",
                },
            ],
        )

    def test_validate_config_uses_ssm_only_fields(self):

        cfg = ec2_runtime.validate_config()

        self.assertEqual(cfg["region"], "us-east-1")

    def test_run_ssm_commands_succeeds(self):
        self.fake_ssm_client.statuses = [
            {"Status": "InProgress"},
            {"Status": "Success", "StandardOutputContent": "ok"},
        ]

        with patch.object(ec2_runtime.time, "sleep"):
            invocation = ec2_runtime._run_ssm_commands(
                self.fake_ssm_client,
                {"ssm_timeout": 10},
                "i-test1",
                ["docker version"],
            )

        self.assertEqual(invocation["Status"], "Success")
        self.assertEqual(
            self.fake_ssm_client.send_requests[0],
            {
                "InstanceIds": ["i-test1"],
                "DocumentName": "AWS-RunShellScript",
                "Parameters": {"commands": ["docker version"]},
            },
        )

    def test_run_ssm_commands_raises_on_failed_status(self):
        self.fake_ssm_client.statuses = [
            {"Status": "Failed", "StandardErrorContent": "boom"}
        ]

        with self.assertRaisesRegex(RuntimeError, "status Failed: boom"):
            ec2_runtime._run_ssm_commands(
                self.fake_ssm_client,
                {"ssm_timeout": 10},
                "i-test1",
                ["docker version"],
            )

    def test_run_ssm_commands_raises_on_timeout(self):
        self.fake_ssm_client.statuses = [{"Status": "InProgress"}]

        with (
            patch.object(ec2_runtime.time, "time", side_effect=[0, 999]),
            patch.object(ec2_runtime.time, "sleep"),
            self.assertRaisesRegex(TimeoutError, "SSM bootstrap timed out"),
        ):
            ec2_runtime._run_ssm_commands(
                self.fake_ssm_client,
                {"ssm_timeout": 1},
                "i-test1",
                ["docker version"],
            )

    def test_build_ssm_bootstrap_commands_for_localstack_uses_container_network(self):
        self.controller.config["ec2"]["endpoint_url"] = "http://localhost:4566"

        commands = ec2_runtime._build_ssm_bootstrap_commands(
            "10.0.0.30",
            {"name": "Tagged", "provider": "EC2", "redis_port": 6390},
            2,
            self.controller.config["ec2"],
            redis_host="10.0.0.30",
            redis_port=6390,
        )

        self.assertIn(
            "INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)",
            commands,
        )
        self.assertTrue(
            any(
                "--network container:localstack-ec2.$INSTANCE_ID" in command
                for command in commands
            )
        )
        self.assertFalse(any("-p 50051:50051" in command for command in commands))

    def test_build_ssm_bootstrap_commands_non_localstack_publishes_port(self):
        self.controller.config["ec2"]["endpoint_url"] = (
            "https://ssm.us-east-1.amazonaws.com"
        )

        commands = ec2_runtime._build_ssm_bootstrap_commands(
            "10.0.0.30",
            {"name": "Tagged", "provider": "EC2", "redis_port": 6390},
            2,
            self.controller.config["ec2"],
            redis_host="10.0.0.30",
            redis_port=6390,
        )

        self.assertTrue(any("-p 50051:50051" in command for command in commands))

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
        self.assertEqual(self.session_calls[-1], self.session_calls[0])

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
