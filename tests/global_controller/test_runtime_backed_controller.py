import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.runtime_fakes import FakeRedis
from tests.support.runtime_fakes import install_grpc_stubs
from tests.support.runtime_fakes import make_global_controller
from tests.support.runtime_fakes import make_instance

install_grpc_stubs()

from ventis.controller.global_controller import GlobalController


class GlobalControllerRuntimeBackedTests(unittest.TestCase):
    def test_load_autoscale_policies_applies_scale_down_defaults(self):
        controller = make_global_controller([])
        controller.config = {"agents": [{"name": "Beta", "replicas": 2}]}

        policies = GlobalController._load_autoscale_policies(
            controller,
            {
                "autoscale": {
                    "Beta": {
                        "queue_length_scale_up_threshold": 3,
                        "max_replicas": 5,
                    }
                }
            },
        )

        self.assertEqual(
            policies,
            {
                "Beta": {
                    "queue_length_scale_up_threshold": 3,
                    "max_replicas": 5,
                    "min_replicas": 2,
                    "idle_seconds_before_scale_down": 60,
                }
            },
        )

    def test_wait_for_healthy_reads_local_status_from_host_redis(self):
        instance = make_instance("Alpha", 0, host="localhost", host_port=8000)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8000:status",
            "healthy",
        )
        published = []
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: published.append(
            [item["name"] for item in controllers]
        )
        controller.controllers = [{"name": "Alpha"}]

        GlobalController._wait_for_healthy(controller, timeout=1, interval=0)

        self.assertEqual(controller._last_status, {("localhost", "8000"): "healthy"})
        self.assertEqual(published, [["Alpha"]])

    def test_poll_controllers_calls_healthy_hook_for_runtime_instance(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8001:status",
            "healthy",
        )

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [("Beta", "localhost", "8001")])
        self.assertEqual(controller._unhealthy_calls, [])

    def test_poll_controllers_calls_unhealthy_hook_for_missing_status(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.node_redis["localhost"] = FakeRedis()

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [])
        self.assertEqual(controller._unhealthy_calls, [("Beta", "localhost", "8001")])
        self.assertEqual(controller._last_status[("localhost", "8001")], "unknown")

    def test_poll_controllers_uses_remote_host_as_status_key(self):
        instance = make_instance("Remote", 0, host="10.0.0.7", host_port=9000)
        controller = make_global_controller([instance])
        controller.node_redis["10.0.0.7"] = FakeRedis()
        controller.node_redis["10.0.0.7"].set(
            "controller:10.0.0.7:9000:status",
            "healthy",
        )

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller._healthy_calls, [("Remote", "10.0.0.7", "9000")])

    def test_update_agent_policy_writes_central_redis_and_only_hosting_nodes(self):
        instances = [
            make_instance("Beta", 0, host="localhost", host_port=8001),
            make_instance("Other", 0, host="10.0.0.7", host_port=9000),
        ]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "priority": 7}, {"name": "Other", "priority": 1}]
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["10.0.0.7"] = FakeRedis()

        result = GlobalController.update_agent_policy(controller, "Beta", {"priority": 2})

        self.assertEqual(result, {"agent": "Beta", "priority": 2, "updated_hosts": ["localhost"]})
        self.assertEqual(controller.redis.hget("agent:Beta:", "priority"), "2")
        self.assertEqual(controller.controllers[0]["priority"], 2)
        self.assertEqual(controller.node_redis["localhost"].hget("agent:Beta:", "priority"), "2")
        self.assertIsNone(controller.node_redis["10.0.0.7"].hget("agent:Beta:", "priority"))

    def test_reconcile_agent_policies_repairs_node_priority_drift(self):
        instances = [
            make_instance("Beta", 0, host="localhost", host_port=8001),
            make_instance("Beta", 1, host="10.0.0.7", host_port=8002),
        ]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "priority": 4}]
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["10.0.0.7"] = FakeRedis()
        controller.redis.hset("agent:Beta:", "priority", "4")
        controller.node_redis["localhost"].hset("agent:Beta:", "priority", "9")
        controller.node_redis["10.0.0.7"].hset("agent:Beta:", "priority", "4")

        updates = GlobalController._reconcile_agent_policies(controller)

        self.assertEqual(updates, {"Beta": ["localhost"]})
        self.assertEqual(controller.node_redis["localhost"].hget("agent:Beta:", "priority"), "4")
        self.assertEqual(controller.node_redis["10.0.0.7"].hget("agent:Beta:", "priority"), "4")

    def test_admin_policy_endpoint_updates_priority(self):
        instances = [make_instance("Beta", 0, host="localhost", host_port=8001)]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "priority": 5}]
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        controller.node_redis["localhost"] = FakeRedis()
        app = GlobalController.create_admin_app(controller)

        response = app.test_client().post("/policy/agents/Beta", json={"priority": -3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"agent": "Beta", "priority": -3, "updated_hosts": ["localhost"]})
        self.assertEqual(controller.redis.hget("agent:Beta:", "priority"), "-3")
        self.assertEqual(controller.node_redis["localhost"].hget("agent:Beta:", "priority"), "-3")

    def test_trigger_cleanup_broadcasts_to_runtime_endpoints(self):
        instance = make_instance("Gamma", 0, host="localhost", host_port=8002)
        controller = make_global_controller([instance])
        controller.redis.sadd("request:completed", "req-1")
        messages = []

        class Stub:
            def Cleanup(self, message):
                messages.append(message)

        controller._get_lc_stub = lambda endpoint: Stub()

        GlobalController._trigger_cleanup(controller)

        self.assertEqual([json.loads(message.resonse) for message in messages], [{"request_id": "req-1"}])
        self.assertEqual(controller.redis.smembers("request:completed"), set())

    def test_trigger_cleanup_noops_without_completed_requests(self):
        instance = make_instance("Gamma", 0, host="localhost", host_port=8002)
        controller = make_global_controller([instance])
        calls = []
        controller._get_lc_stub = lambda endpoint: calls.append(endpoint)

        GlobalController._trigger_cleanup(controller)

        self.assertEqual(calls, [])

    def test_stop_docker_agents_delegates_to_runtime_manager(self):
        instance = make_instance("Delta", 0, host="localhost", host_port=8003)
        controller = make_global_controller([instance])
        controller.containers = {"Delta": [instance["runtime_id"]]}
        removed = []
        controller.runtime_manager.remove_instance = lambda instance_id: removed.append(instance_id)

        GlobalController._stop_docker_agents(controller)

        self.assertEqual(removed, ["local:Delta:0"])
        self.assertEqual(controller._run_cmd_calls, [])
        self.assertEqual(controller.containers, {})

    def test_agent_host_key_maps_localhost_for_container_status(self):
        controller = make_global_controller([])

        self.assertEqual(
            GlobalController._agent_host_key(controller, "localhost"),
            "host.docker.internal",
        )
        self.assertEqual(
            GlobalController._agent_host_key(controller, "10.0.0.4"),
            "10.0.0.4",
        )

    def test_launch_redis_containers_uses_runtime_nodes(self):
        controller = make_global_controller([])
        controller.runtime_manager.list_runtime_nodes = lambda agent_specs=None: {
            "localhost": {"user": None, "redis_port": 6379},
            "10.0.0.7": {"user": "ubuntu", "redis_port": 6380},
        }
        created_clients = []

        with patch(
            "ventis.controller.global_controller.RedisClient",
            side_effect=lambda **kwargs: created_clients.append(kwargs) or FakeRedis(),
        ):
            GlobalController._launch_redis_containers(controller)

        self.assertEqual(
            controller.redis_containers,
            {
                "localhost": "ventis-redis-localhost",
                "10.0.0.7": "ventis-redis-10-0-0-7",
            },
        )
        self.assertEqual(created_clients, [{"host": "localhost", "port": 6379}, {"host": "10.0.0.7", "port": 6380}])

    def test_ensure_host_redis_reuses_existing_client(self):
        controller = make_global_controller([])
        existing = FakeRedis()
        controller.node_redis["localhost"] = existing

        result = GlobalController.ensure_host_redis(controller, "localhost")

        self.assertIs(result, existing)
        self.assertEqual(controller._run_cmd_calls, [])

    def test_ensure_host_redis_exits_when_docker_run_fails(self):
        controller = make_global_controller([])
        controller._run_cmd = lambda cmd, host, user=None: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="docker failed",
        )

        with self.assertRaises(SystemExit):
            GlobalController.ensure_host_redis(controller, "localhost")

    def test_ssh_base_cmd_uses_configured_private_key(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}

        self.assertEqual(
            GlobalController._ssh_base_cmd(controller),
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-i",
                "/tmp/test.pem",
            ],
        )

    def test_run_cmd_uses_ssh_options_for_remote_host(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}

        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run_mock:
            GlobalController._run_cmd(controller, ["docker", "ps"], "10.0.0.7", "ec2-user")

        run_mock.assert_called_once_with(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-i",
                "/tmp/test.pem",
                "ec2-user@10.0.0.7",
                "sudo docker ps",
            ],
            capture_output=True,
            text=True,
        )

    def test_ensure_host_redis_prepares_remote_docker_first(self):
        controller = make_global_controller([])
        calls = []
        controller._ensure_remote_docker = lambda host, user=None: calls.append(
            ("prep", host, user)
        ) or SimpleNamespace(returncode=0, stdout="", stderr="")
        controller._run_cmd = lambda cmd, host, user=None: calls.append(
            ("run", cmd, host, user)
        ) or SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "ventis.controller.global_controller.RedisClient",
            side_effect=lambda **kwargs: FakeRedis(),
        ):
            GlobalController.ensure_host_redis(controller, "10.0.0.7", "ec2-user", 6380)

        self.assertEqual(calls[0], ("prep", "10.0.0.7", "ec2-user"))
        self.assertEqual(calls[1][0], "run")

    def test_poll_controllers_scales_service_when_queue_depth_exceeds_threshold(self):
        instances = [make_instance("Beta", 0, host="localhost", host_port=8001)]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 1}]
        controller._autoscale_policies = {
            "Beta": {"queue_length_scale_up_threshold": 3, "max_replicas": 2}
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "controller:host.docker.internal:8001:status",
            "healthy",
        )
        controller.node_redis["localhost"].set(
            "queue_depth:Beta:host.docker.internal:8001",
            "4",
        )
        publish_calls = []
        waited = []

        def ensure_replica(_controller_spec, replica_index, publish=True):
            self.assertFalse(publish)
            self.assertEqual(replica_index, 1)
            instances.append(make_instance("Beta", 1, host="localhost", host_port=8002))
            return instances[-1]

        controller.runtime_manager.ensure_replica = ensure_replica
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        controller.runtime_manager.remove_instance = lambda instance_id, publish=True: None
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: publish_calls.append(
            [ctrl["name"] for ctrl in controllers]
        )
        controller._wait_for_pending_healthy = lambda pending, timeout=30, interval=2, require_healthy=False: waited.extend(pending)

        GlobalController._poll_controllers(controller)

        self.assertEqual(controller.controllers[0]["replicas"], 2)
        self.assertEqual(waited, [("Beta", "localhost", "8002")])
        self.assertTrue(publish_calls)
        self.assertEqual(publish_calls[-1], ["Beta"])

    def test_reconcile_marks_instance_idling_only_after_idle_window(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.controllers = [{"name": "Beta", "replicas": 1}]
        controller._autoscale_policies = {
            "Beta": {
                "queue_length_scale_up_threshold": 3,
                "max_replicas": 2,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 60,
            }
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:status", "healthy")
        controller.node_redis["localhost"].set("queue_depth:Beta:host.docker.internal:8001", "0")
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:active_work", "0")
        controller._last_status[("localhost", "8001")] = "healthy"
        publish_calls = []
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: publish_calls.append(kwargs)

        with patch("ventis.controller.global_controller.time.time", return_value=100):
            GlobalController._reconcile_instance_lifecycle(controller)
        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Healthy")

        with patch("ventis.controller.global_controller.time.time", return_value=161):
            GlobalController._reconcile_instance_lifecycle(controller)
        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Idling")
        self.assertTrue(publish_calls)

    def test_reconcile_resets_idling_when_queue_depth_returns(self):
        instance = make_instance("Beta", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.controllers = [{"name": "Beta", "replicas": 1}]
        controller._autoscale_policies = {
            "Beta": {
                "queue_length_scale_up_threshold": 3,
                "max_replicas": 2,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 1,
            }
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set("queue_depth:Beta:host.docker.internal:8001", "0")
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:active_work", "0")
        controller._last_status[("localhost", "8001")] = "healthy"
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: None

        with patch("ventis.controller.global_controller.time.time", return_value=10):
            GlobalController._reconcile_instance_lifecycle(controller)
        with patch("ventis.controller.global_controller.time.time", return_value=12):
            GlobalController._reconcile_instance_lifecycle(controller)
        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Idling")

        controller.node_redis["localhost"].set("queue_depth:Beta:host.docker.internal:8001", "2")
        with patch("ventis.controller.global_controller.time.time", return_value=13):
            GlobalController._reconcile_instance_lifecycle(controller)
        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Healthy")
        self.assertNotIn("host.docker.internal:8001", controller._idle_since)

    def test_reconcile_removes_endpoint_before_marking_shutting_down(self):
        instances = [
            make_instance("Beta", 0, host="localhost", host_port=8001),
            make_instance("Beta", 1, host="localhost", host_port=8002),
        ]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 2}]
        controller._autoscale_policies = {
            "Beta": {
                "queue_length_scale_up_threshold": 3,
                "max_replicas": 3,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 1,
            }
        }
        controller.node_redis["localhost"] = FakeRedis()
        for port in ("8001", "8002"):
            controller.node_redis["localhost"].set(f"queue_depth:Beta:host.docker.internal:{port}", "0")
            controller.node_redis["localhost"].set(f"controller:host.docker.internal:{port}:active_work", "0")
            controller._last_status[("localhost", port)] = "healthy"

        publish_calls = []
        controller.runtime_manager.publish_routing_snapshot = (
            lambda controllers, **kwargs: publish_calls.append(kwargs)
        )

        with patch("ventis.controller.global_controller.time.time", return_value=10):
            GlobalController._reconcile_instance_lifecycle(controller)
        with patch("ventis.controller.global_controller.time.time", return_value=12):
            GlobalController._reconcile_instance_lifecycle(controller)

        self.assertGreaterEqual(len(publish_calls), 2)
        first = publish_calls[-2]
        second = publish_calls[-1]
        self.assertEqual(first["routable_endpoints"]["Beta"], {"host.docker.internal:8001"})
        self.assertEqual(
            first["lifecycle_statuses"]["Beta"]["host.docker.internal:8002"],
            "Idling",
        )
        self.assertEqual(
            second["lifecycle_statuses"]["Beta"]["host.docker.internal:8002"],
            "Shutting down",
        )

    def test_stateful_instance_does_not_idle_with_affine_running_request(self):
        instance = make_instance("Sticky", 0, host="localhost", host_port=8001)
        controller = make_global_controller([instance])
        controller.controllers = [{"name": "Sticky", "replicas": 1, "stateful": True}]
        controller._autoscale_policies = {
            "Sticky": {
                "queue_length_scale_up_threshold": 3,
                "max_replicas": 2,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 1,
            }
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set("queue_depth:Sticky:host.docker.internal:8001", "0")
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:active_work", "0")
        controller._last_status[("localhost", "8001")] = "healthy"
        controller.redis.hset("affinity:req-1", "Sticky", "host.docker.internal:8001")
        controller.redis.set("request:req-1:status", "running")
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: None

        with patch("ventis.controller.global_controller.time.time", return_value=10):
            GlobalController._reconcile_instance_lifecycle(controller)
        with patch("ventis.controller.global_controller.time.time", return_value=12):
            GlobalController._reconcile_instance_lifecycle(controller)

        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Healthy")

        controller.redis.set("request:req-1:status", "done")
        with patch("ventis.controller.global_controller.time.time", return_value=14):
            GlobalController._reconcile_instance_lifecycle(controller)
        with patch("ventis.controller.global_controller.time.time", return_value=16):
            GlobalController._reconcile_instance_lifecycle(controller)
        self.assertEqual(controller._lifecycle_statuses["host.docker.internal:8001"], "Idling")

    def test_delete_ready_removes_runtime_and_metadata(self):
        instances = [make_instance("Beta", 0, host="localhost", host_port=8001)]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 2}]
        controller._autoscale_policies = {
            "Beta": {
                "queue_length_scale_up_threshold": 3,
                "max_replicas": 3,
                "min_replicas": 1,
                "idle_seconds_before_scale_down": 1,
            }
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:active_work", "0")
        controller.node_redis["localhost"].set("queue_depth:Beta:host.docker.internal:8001", "0")
        controller.node_redis["localhost"].set("controller:host.docker.internal:8001:lifecycle", "Delete Ready")
        controller._last_status[("localhost", "8001")] = "healthy"
        controller._draining_endpoints.add("host.docker.internal:8001")
        removed = []
        controller.runtime_manager.remove_instance = lambda instance_id, publish=False: removed.append((instance_id, publish))
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: None

        GlobalController._reconcile_instance_lifecycle(controller)

        self.assertEqual(removed, [("local:Beta:0", False)])
        self.assertNotIn("host.docker.internal:8001", controller._lifecycle_statuses)

    def test_poll_controllers_does_not_scale_beyond_max_replicas(self):
        instances = [
            make_instance("Beta", 0, host="localhost", host_port=8001),
            make_instance("Beta", 1, host="localhost", host_port=8002),
        ]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 2}]
        controller._autoscale_policies = {
            "Beta": {"queue_length_scale_up_threshold": 3, "max_replicas": 2}
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "queue_depth:Beta:host.docker.internal:8001",
            "99",
        )
        controller.node_redis["localhost"].set(
            "queue_depth:Beta:host.docker.internal:8002",
            "99",
        )
        calls = []
        controller.runtime_manager.ensure_instances = lambda controllers, publish=True: calls.append(
            (controllers, publish)
        )

        GlobalController._scale_from_queue_depth(controller)

        self.assertEqual(calls, [])

    def test_scale_up_timeout_rolls_back_after_publishing_initializing_status(self):
        instances = [make_instance("Beta", 0, host="localhost", host_port=8001)]
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 1}]
        controller._autoscale_policies = {
            "Beta": {"queue_length_scale_up_threshold": 3, "max_replicas": 2}
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "queue_depth:Beta:host.docker.internal:8001",
            "4",
        )
        publish_calls = []
        removed = []

        def ensure_replica(_controller_spec, replica_index, publish=True):
            self.assertEqual(replica_index, 1)
            instances.append(make_instance("Beta", 1, host="localhost", host_port=8002))
            return instances[-1]

        def remove_instance(instance_id, publish=True):
            removed.append((instance_id, publish))
            instances[:] = [
                instance for instance in instances
                if controller.runtime_manager._instance_id_from_record(instance) != instance_id
            ]

        controller.runtime_manager.ensure_replica = ensure_replica
        controller.runtime_manager.remove_instance = remove_instance
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: publish_calls.append(
            [ctrl["name"] for ctrl in controllers]
        )
        controller._wait_for_pending_healthy = lambda pending, timeout=30, interval=2, require_healthy=False: (_ for _ in ()).throw(TimeoutError("not ready"))

        with self.assertRaises(TimeoutError):
            GlobalController._scale_from_queue_depth(controller)

        self.assertEqual(controller.controllers[0]["replicas"], 1)
        self.assertEqual(removed, [("local:Beta:1", False)])
        self.assertEqual(publish_calls, [["Beta"]])

    def test_scale_up_timeout_only_rolls_back_new_replica_slot(self):
        instances = [make_instance("Beta", 0, host="localhost", host_port=8001)]
        instances[0]["runtime_id"] = "stale-runtime"
        controller = make_global_controller(instances)
        controller.controllers = [{"name": "Beta", "replicas": 1}]
        controller._autoscale_policies = {
            "Beta": {"queue_length_scale_up_threshold": 3, "max_replicas": 2}
        }
        controller.node_redis["localhost"] = FakeRedis()
        controller.node_redis["localhost"].set(
            "queue_depth:Beta:host.docker.internal:8001",
            "4",
        )
        removed = []

        def ensure_replica(_controller_spec, replica_index, publish=True):
            self.assertEqual(replica_index, 1)
            instances.append(make_instance("Beta", 1, host="localhost", host_port=8002))
            return instances[-1]

        def remove_instance(instance_id, publish=True):
            removed.append((instance_id, publish))
            instances[:] = [
                instance for instance in instances
                if controller.runtime_manager._instance_id_from_record(instance) != instance_id
            ]

        controller.runtime_manager.ensure_replica = ensure_replica
        controller.runtime_manager.remove_instance = remove_instance
        controller.runtime_manager.list_instances = lambda agent_name=None: [
            instance for instance in instances if agent_name in (None, instance["agent_name"])
        ]
        publish_calls = []
        controller.runtime_manager.publish_routing_snapshot = lambda controllers, **kwargs: publish_calls.append(
            [ctrl["name"] for ctrl in controllers]
        )
        controller._wait_for_pending_healthy = lambda pending, timeout=30, interval=2, require_healthy=False: (_ for _ in ()).throw(TimeoutError("not ready"))

        with self.assertRaises(TimeoutError):
            GlobalController._scale_from_queue_depth(controller)

        self.assertEqual(controller.controllers[0]["replicas"], 1)
        self.assertEqual(
            removed,
            [("local:Beta:1", False)],
        )
        self.assertEqual(publish_calls, [["Beta"]])

    def test_wait_for_remote_ssh_retries_until_success(self):
        controller = make_global_controller([])
        controller.config = {"ec2": {"ssh_private_key_path": "/tmp/test.pem"}}
        results = [
            SimpleNamespace(returncode=255, stdout="", stderr="Connection timed out"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]

        with patch("subprocess.run", side_effect=results) as run_mock:
            with patch("time.sleep", return_value=None):
                result = GlobalController._wait_for_remote_ssh(
                    controller, "10.0.0.7", "ec2-user", timeout=5, interval=0
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
