import json
import logging

from ventis.controller.cloud_provider_logic.EC2 import _runtime as ec2_runtime

logger = logging.getLogger(__name__)
DEFAULT_HOST = "localhost"
DEFAULT_HOST_PORT_START = 8000


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def _container_routing_host(host):
    return "host.docker.internal" if _is_local_host(host) else host


def reject_legacy_replica_shape(agent_spec):
    if "host" in agent_spec or "port" in agent_spec or isinstance(agent_spec.get("replicas"), list):
        raise ValueError("Legacy YAML host/port replica placement is no longer supported; use provider with integer replicas only.")


def resolve_local_replica_placements(agent_spec):
    reject_legacy_replica_shape(agent_spec)
    replicas = int(agent_spec.get("replicas", 1))
    return [{"host": DEFAULT_HOST, "host_port": None} for _ in range(replicas)]


def allocate_host_port(runtime_manager, host, requested_host_port=None, ignore_instance_id=None):
    if requested_host_port is not None:
        return int(requested_host_port)

    used_ports = set()
    for instance in runtime_manager.list_instances():
        if ignore_instance_id and runtime_manager._instance_id_from_record(instance) == ignore_instance_id:
            continue
        if instance.get("host") != host:
            continue
        host_port = instance.get("host_port")
        if host_port is not None:
            used_ports.add(int(host_port))

    host_port = DEFAULT_HOST_PORT_START
    while host_port in used_ports:
        host_port += 1
    return host_port


class RuntimeManager:
    """Create, reuse, and publish agent runtimes."""

    ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
    ROUTING_STATUS_KEY = "routing_table:status"
    ROUTING_STATEFUL_KEY = "routing_table:stateful"
    SERVICES_SET_KEY = "routing_table:services"
    CONTAINER_PORT = 50051
    WORKFLOW_API_PORT = 8080

    def __init__(self, controller, redis_client=None):
        self.controller = controller
        self._redis = redis_client

    @property
    def redis(self):
        return self._redis or self.controller.redis

    def ensure_instances(self, agent_specs, publish=True):
        instances = []
        self._agent_specs = list(agent_specs)
        if publish:
            self.publish_routing_snapshot(self._agent_specs)
        ec2_runtime._set_controller(self.controller)

        for agent_spec in agent_specs:
            agent_name = agent_spec["name"]
            provider = agent_spec.get("provider", "local")
            self.controller.containers.setdefault(agent_name, [])

            if provider.upper() == "EC2":
                ec2_runtime.validate_config()
                for replica_index in range(int(agent_spec.get("replicas", 1))):
                    instance_id = self._instance_id(provider, agent_name, replica_index)
                    key = self._instance_key(provider, agent_name, replica_index)
                    instance = self.redis.hgetall(key)
                    if instance:
                        self.remove_instance(instance_id, publish=publish)
                    provisioned = ec2_runtime.provision_instance(agent_spec, replica_index)
                    redis_port = int(agent_spec.get("redis_port", provisioned.get("redis_port", 6379)))
                    self.controller.ensure_host_redis(
                        provisioned["host"],
                        provisioned.get("user"),
                        redis_port,
                        ssh_host=provisioned.get("ssh_host"),
                    )
                    if publish:
                        self.publish_routing_snapshot(self._agent_specs)
                    instance = ec2_runtime.bootstrap_instance(
                        provisioned,
                        agent_spec,
                        replica_index,
                        redis_host=provisioned["host"],
                        redis_port=redis_port,
                    )
                    self._write_instance(instance)
                    self._add_instance_to_agent(agent_name, instance_id)
                    self._track_runtime(agent_name, instance["runtime_id"])
                    if publish:
                        self.publish_routing_snapshot(self._agent_specs)
                    instances.append(instance)
                continue

            for replica_index, placement in enumerate(self._replica_placements(agent_spec)):
                host = placement["host"]
                host_port = placement.get("host_port")
                instance_id = self._instance_id(provider, agent_name, replica_index)
                key = self._instance_key(provider, agent_name, replica_index)
                instance = self.redis.hgetall(key)

                if instance and self._runtime_exists(instance) and self._placement_matches(instance, host, host_port):
                    pass
                else:
                    if instance:
                        self.remove_instance(instance_id, publish=publish)
                    instance = self._create_instance(
                        agent_spec=agent_spec,
                        host=host,
                        host_port=host_port,
                        replica_index=replica_index,
                        instance_id=instance_id,
                        previous_instance=instance,
                    )
                    self._write_instance(instance)

                self._add_instance_to_agent(agent_name, instance_id)
                self._track_runtime(agent_name, instance["runtime_id"])
                if publish:
                    self.publish_routing_snapshot(self._agent_specs)
                instances.append(instance)

        return instances

    def ensure_replica(self, agent_spec, replica_index, publish=True):
        """Create or reuse a single replica slot without re-provisioning siblings.

        Used for autoscale scale-up. Callers typically pass publish=False and
        publish routing through GlobalController._publish_routing_state() so
        lifecycle status (Initializing/Healthy) is included.
        """
        self._agent_specs = list(self._current_agent_specs())
        if publish:
            self.publish_routing_snapshot(self._agent_specs)
        ec2_runtime._set_controller(self.controller)

        agent_name = agent_spec["name"]
        provider = agent_spec.get("provider", "local")
        self.controller.containers.setdefault(agent_name, [])
        instance_id = self._instance_id(provider, agent_name, replica_index)
        key = self._instance_key(provider, agent_name, replica_index)
        instance = self.redis.hgetall(key)

        if provider.upper() == "EC2":
            ec2_runtime.validate_config()
            if instance:
                self.remove_instance(instance_id, publish=publish)
            provisioned = ec2_runtime.provision_instance(agent_spec, replica_index)
            redis_port = int(agent_spec.get("redis_port", provisioned.get("redis_port", 6379)))
            self.controller.ensure_host_redis(
                provisioned["host"],
                provisioned.get("user"),
                redis_port,
                ssh_host=provisioned.get("ssh_host"),
            )
            if publish:
                self.publish_routing_snapshot(self._agent_specs)
            instance = ec2_runtime.bootstrap_instance(
                provisioned,
                agent_spec,
                replica_index,
                redis_host=provisioned["host"],
                redis_port=redis_port,
            )
            self._write_instance(instance)
            self._add_instance_to_agent(agent_name, instance_id)
            self._track_runtime(agent_name, instance["runtime_id"])
            if publish:
                self.publish_routing_snapshot(self._agent_specs)
            return instance

        placement = self._replica_placements(agent_spec)[replica_index]
        host = placement["host"]
        host_port = placement.get("host_port")
        if instance and self._runtime_exists(instance) and self._placement_matches(instance, host, host_port):
            return instance
        if instance:
            self.remove_instance(instance_id, publish=publish)
        instance = self._create_instance(
            agent_spec=agent_spec,
            host=host,
            host_port=host_port,
            replica_index=replica_index,
            instance_id=instance_id,
            previous_instance=instance,
        )
        self._write_instance(instance)
        self._add_instance_to_agent(agent_name, instance_id)
        self._track_runtime(agent_name, instance["runtime_id"])
        if publish:
            self.publish_routing_snapshot(self._agent_specs)
        return instance

    def _write_instance(self, instance):
        key = self._instance_key(
            instance["provider"],
            instance["agent_name"],
            int(instance["replica_index"]),
        )
        mapping = {
            "agent_name": instance["agent_name"],
            "provider": instance["provider"],
            "replica_index": str(instance["replica_index"]),
            "host": instance["host"],
            "host_port": str(instance["host_port"]),
            "container_port": str(instance["container_port"]),
            "endpoint": instance["endpoint"],
            "redis_host": instance["redis_host"],
            "redis_port": str(instance["redis_port"]),
            "runtime_id": instance["runtime_id"],
        }
        if instance.get("ec2_instance_id"):
            mapping["ec2_instance_id"] = instance["ec2_instance_id"]
        self.redis.hset_multiple(key, mapping)

    def _add_instance_to_agent(self, agent_name, instance_id):
        self.redis.sadd(f"agent:{agent_name}:instances", instance_id)

    def remove_instance(self, instance_id, publish=True):
        key = f"agent_instance:{instance_id}"
        instance = self.redis.hgetall(key)
        if not instance:
            return

        self._destroy_runtime(instance)
        self.redis.delete(key)
        self.redis.srem(f"agent:{instance['agent_name']}:instances", instance_id)

        if publish:
            self.publish_routing_snapshot(self._current_agent_specs())

        containers = self.controller.containers.get(instance["agent_name"], [])
        self.controller.containers[instance["agent_name"]] = [
            runtime_id
            for runtime_id in containers
            if runtime_id != instance["runtime_id"]
        ]

    def list_instances(self, agent_name=None):
        if agent_name:
            instance_ids = sorted(self.redis.smembers(f"agent:{agent_name}:instances"))
            instances = []
            for instance_id in instance_ids:
                instance = self.redis.hgetall(f"agent_instance:{instance_id}")
                if instance:
                    instances.append(instance)
            return instances

        instances = []
        for key in sorted(self.redis.scan_keys("agent_instance:*")):
            instance = self.redis.hgetall(key)
            if instance:
                instances.append(instance)
        return instances

    def _create_instance(
        self,
        agent_spec,
        host,
        host_port,
        replica_index,
        instance_id=None,
        previous_instance=None,
    ):
        provider = agent_spec.get("provider", "local")
        host_port = self._resolve_host_port(
            agent_spec,
            host,
            host_port,
            instance_id=instance_id,
            previous_instance=previous_instance,
        )
        return self._create_local_instance(agent_spec, host, host_port, replica_index)

    def _create_local_instance(self, agent_spec, host, host_port, replica_index):
        return self._launch_container(
            agent_spec=agent_spec,
            host=host,
            host_port=host_port,
            replica_index=replica_index,
            ensure_remote_image=False,
        )

    def _launch_container(self, agent_spec, host, host_port, replica_index, ensure_remote_image):
        agent_name = agent_spec["name"]
        provider = agent_spec.get("provider", "local")
        user = agent_spec.get("user")
        resources = agent_spec.get("resources", {})
        ctrl_type = agent_spec.get("type", "agent")
        image = f"ventis-{agent_name.lower()}"
        runtime_id = f"ventis-{provider.lower()}-{agent_name.lower()}-{replica_index}"
        redis_host = _container_routing_host(host)
        agent_host = redis_host

        cmd = [
            "docker",
            "run",
            "-d",
            "-it",
            "--add-host=host.docker.internal:host-gateway",
            "--name",
            runtime_id,
            "-p",
            f"{host_port}:{self.CONTAINER_PORT}",
            "-e",
            f"VENTIS_AGENT_PORT={host_port}",
            "-e",
            f"VENTIS_AGENT_HOST={agent_host}",
            "-e",
            f"VENTIS_REDIS_HOST={redis_host}",
            "-e",
            f"VENTIS_REDIS_PORT={agent_spec.get('redis_port', 6379)}",
            "-e",
            f"VENTIS_AGENT_PRIORITY={agent_spec.get('priority', 0)}",
        ]

        if ctrl_type == "workflow":
            cmd.extend(["-p", f"{self.WORKFLOW_API_PORT}:8080"])

        cpu = resources.get("cpu")
        memory = resources.get("memory")
        gpu = resources.get("gpu")
        if cpu:
            cmd.extend(["--cpus", str(cpu)])
        if memory:
            cmd.extend(["--memory", f"{memory}m"])
        if gpu:
            cmd.extend(["--gpus", str(gpu)])

        cmd.append(image)

        if ensure_remote_image:
            self.controller._ensure_image_on_host(image, host, user)

        result = self.controller._run_cmd(cmd, host, user)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to launch {runtime_id} on {host}:{host_port}: {result.stderr.strip()}"
            )

        instance = {
            "agent_name": agent_name,
            "provider": provider,
            "replica_index": str(replica_index),
            "host": host,
            "host_port": str(host_port),
            "container_port": str(self.CONTAINER_PORT),
            "endpoint": f"{host}:{host_port}",
            "redis_host": redis_host,
            "redis_port": str(agent_spec.get("redis_port", 6379)),
            "runtime_id": runtime_id,
        }
        logger.info("Runtime ready: %s -> %s", runtime_id, instance["endpoint"])
        return instance

    def _runtime_exists(self, instance):
        runtime_id = instance.get("runtime_id")
        if not runtime_id:
            return False
        if instance.get("provider", "local").upper() == "EC2":
            try:
                return bool(ec2_runtime._get_instance_host(runtime_id))
            except Exception:
                return False
        host = instance.get("host", "localhost")
        result = self.controller._run_cmd(
            ["docker", "inspect", runtime_id],
            host,
            self._user_for_instance(instance),
        )
        return result.returncode == 0

    def _destroy_runtime(self, instance):
        runtime_id = instance.get("runtime_id")
        if not runtime_id:
            return
        if instance.get("provider", "local").upper() == "EC2":
            ec2_runtime.terminate_instance(runtime_id)
            host = instance.get("host")
            if host:
                getattr(self.controller, "redis_containers", {}).pop(host, None)
                getattr(self.controller, "node_redis", {}).pop(host, None)
            return
        result = self.controller._run_cmd(
            ["docker", "rm", "-f", runtime_id],
            instance.get("host", "localhost"),
            self._user_for_instance(instance),
        )
        if result.returncode != 0:
            logger.warning("Failed to remove runtime %s: %s", runtime_id, result.stderr.strip())

    def _track_runtime(self, agent_name, runtime_id):
        containers = self.controller.containers.setdefault(agent_name, [])
        if runtime_id not in containers:
            containers.append(runtime_id)

    def _replica_placements(self, agent_spec):
        provider = agent_spec.get("provider", "local")
        if provider.upper() == "EC2":
            reject_legacy_replica_shape(agent_spec)
            return [None] * int(agent_spec.get("replicas", 1))
        return resolve_local_replica_placements(agent_spec)

    def list_runtime_nodes(self, agent_specs=None):
        nodes = {}
        for agent_spec in agent_specs or getattr(self.controller, "controllers", []):
            if agent_spec.get("provider", "local").upper() == "EC2":
                continue
            user = agent_spec.get("user")
            redis_port = agent_spec.get("redis_port", 6379)
            for placement in self._replica_placements(agent_spec):
                host = placement["host"]
                nodes.setdefault(host, {"user": user, "redis_port": redis_port})
        return nodes

    def _resolve_host_port(
        self,
        agent_spec,
        host,
        requested_host_port,
        instance_id=None,
        previous_instance=None,
    ):
        if previous_instance and previous_instance.get("host") == host and previous_instance.get("host_port"):
            requested_host_port = previous_instance["host_port"]
        return allocate_host_port(
            self,
            host,
            requested_host_port=requested_host_port,
            ignore_instance_id=instance_id,
        )

    def _placement_matches(self, instance, host, host_port):
        if instance.get("host") != host:
            return False
        if host_port is None:
            return True
        return str(instance.get("host_port")) == str(host_port)

    def _routing_endpoint_for(self, instance):
        if instance.get("provider", "local").lower() == "local" and _is_local_host(instance.get("host")):
            return f"host.docker.internal:{instance['host_port']}"
        return instance["endpoint"]

    @staticmethod
    def _instance_id(provider, agent_name, replica_index):
        return f"{provider}:{agent_name}:{replica_index}"

    @classmethod
    def _instance_key(cls, provider, agent_name, replica_index):
        return f"agent_instance:{cls._instance_id(provider, agent_name, replica_index)}"

    def sync_routing_metadata(self, agent_specs):
        self.publish_routing_snapshot(agent_specs)

    def publish_routing_snapshot(self, agent_specs, lifecycle_statuses=None, routable_endpoints=None):
        """Copy routing metadata derived from central records to host Redis.

        lifecycle_statuses maps service -> {endpoint: status} for routing_table:status.
        routable_endpoints maps service -> set of endpoints that accept new traffic;
        endpoints omitted from routable_endpoints stay in status but are excluded
        from routing_table:endpoints (used during scale-down draining).
        """
        services = {agent_spec["name"] for agent_spec in agent_specs}
        stateful = {
            agent_spec["name"]
            for agent_spec in agent_specs
            if agent_spec.get("stateful", False)
        }
        lifecycle_statuses = lifecycle_statuses or {}
        routable_endpoints = routable_endpoints or {}

        for redis_client in self._routing_redis_targets():
            existing_services = redis_client.smembers(self.SERVICES_SET_KEY)
            for stale in existing_services - services:
                redis_client.srem(self.SERVICES_SET_KEY, stale)
                self._hdel(redis_client, self.ROUTING_STATEFUL_KEY, stale)
                self._hdel(redis_client, self.ROUTING_ENDPOINTS_KEY, stale)
                self._hdel(redis_client, self.ROUTING_STATUS_KEY, stale)
            for service in services:
                redis_client.sadd(self.SERVICES_SET_KEY, service)
                if service in stateful:
                    redis_client.hset(self.ROUTING_STATEFUL_KEY, service, "true")
                else:
                    self._hdel(redis_client, self.ROUTING_STATEFUL_KEY, service)
                service_instances = sorted(
                    self.list_instances(service),
                    key=lambda item: int(item["replica_index"]),
                )
                endpoints = [
                    self._routing_endpoint_for(item)
                    for item in service_instances
                    if self._routing_endpoint_for(item) in routable_endpoints.get(service, {
                        self._routing_endpoint_for(instance) for instance in service_instances
                    })
                ]
                if endpoints:
                    redis_client.hset(
                        self.ROUTING_ENDPOINTS_KEY,
                        service,
                        json.dumps(endpoints),
                    )
                else:
                    self._hdel(redis_client, self.ROUTING_ENDPOINTS_KEY, service)
                statuses = lifecycle_statuses.get(service)
                if statuses is None:
                    statuses = {
                        self._routing_endpoint_for(item): "Healthy"
                        for item in service_instances
                    }
                if statuses:
                    redis_client.hset(
                        self.ROUTING_STATUS_KEY,
                        service,
                        json.dumps(statuses),
                    )
                else:
                    self._hdel(redis_client, self.ROUTING_STATUS_KEY, service)

    def publish_policy_rules(self, rules):
        rules_json = json.dumps(rules)
        targets = self._routing_redis_targets()
        for redis_client in targets:
            redis_client.set("policy:rules", rules_json)
        return len(targets)

    def _hdel(self, redis_client, name, field):
        if hasattr(redis_client, "client"):
            redis_client.client.hdel(name, field)
            return
        if hasattr(redis_client, "hdel"):
            redis_client.hdel(name, field)

    def _instance_id_from_record(self, instance):
        return self._instance_id(
            instance["provider"],
            instance["agent_name"],
            int(instance["replica_index"]),
        )

    def _user_for_instance(self, instance):
        agent_name = instance.get("agent_name")
        for agent_spec in getattr(self.controller, "controllers", []):
            if agent_spec.get("name") == agent_name:
                return agent_spec.get("user")
        return None

    def _current_agent_specs(self):
        return getattr(self, "_agent_specs", getattr(self.controller, "controllers", []))

    def _routing_redis_targets(self):
        targets = list(getattr(self.controller, "node_redis", {}).values())
        return targets or [self.redis]
