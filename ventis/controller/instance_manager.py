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


class InstanceManager:
    ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
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

    def ensure_instances(self, agent_specs):
        self._agent_specs = list(agent_specs)
        ec2_runtime._controller = self.controller
        instances = []

        for agent_spec in self._agent_specs:
            agent_name = agent_spec["name"]
            provider = agent_spec.get("provider", "local")
            self.controller.containers.setdefault(agent_name, [])

            if provider.upper() == "EC2":
                ec2_runtime.validate_config()

            for replica_index in range(int(agent_spec.get("replicas", 1))):
                instance_id = self._instance_id(provider, agent_name, replica_index)
                key = self._instance_key(provider, agent_name, replica_index)
                instance = self.redis.hgetall(key)

                if not instance:
                    if provider.upper() == "EC2":
                        provisioned = ec2_runtime.provision_instance(agent_spec, replica_index)
                        redis_port = int(agent_spec.get("redis_port", provisioned.get("redis_port", 6379)))
                        instance = ec2_runtime.bootstrap_instance(
                            provisioned,
                            agent_spec,
                            replica_index,
                            redis_host=provisioned["host"],
                            redis_port=redis_port,
                        )
                    else:
                        host = agent_spec.get("host", DEFAULT_HOST)
                        host_port = int(agent_spec.get("host_port", agent_spec.get("port", self._next_host_port(host))))
                        instance = self._launch_container(agent_spec, host, host_port, replica_index, ensure_remote_image=False)
                    self._write_instance(instance)

                self._add_instance_to_agent(agent_name, instance_id)
                self._track_runtime(agent_name, instance["runtime_id"])
                instances.append(instance)

        self.publish_routing_snapshot(self._agent_specs)
        return instances

    def _write_instance(self, instance):
        key = self._instance_key(instance["provider"], instance["agent_name"], int(instance["replica_index"]))
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
        if instance.get("user"):
            mapping["user"] = instance["user"]
        self.redis.hset_multiple(key, mapping)

    def _add_instance_to_agent(self, agent_name, instance_id):
        self.redis.sadd(f"agent:{agent_name}:instances", instance_id)

    def remove_instance(self, instance_id):
        key = f"agent_instance:{instance_id}"
        instance = self.redis.hgetall(key)
        if not instance:
            return

        self._destroy_runtime(instance)
        self.redis.delete(key)
        self.redis.srem(f"agent:{instance['agent_name']}:instances", instance_id)
        self.controller.containers[instance["agent_name"]] = [
            runtime_id
            for runtime_id in self.controller.containers.get(instance["agent_name"], [])
            if runtime_id != instance["runtime_id"]
        ]
        self.publish_routing_snapshot(getattr(self, "_agent_specs", getattr(self.controller, "controllers", [])))

    def list_instances(self, agent_name=None):
        if agent_name:
            instance_ids = sorted(self.redis.smembers(f"agent:{agent_name}:instances"))
            return [instance for instance_id in instance_ids if (instance := self.redis.hgetall(f"agent_instance:{instance_id}"))]

        return [instance for key in sorted(self.redis.scan_keys("agent_instance:*")) if (instance := self.redis.hgetall(key))]

    def _launch_container(self, agent_spec, host, host_port, replica_index, ensure_remote_image):
        agent_name = agent_spec["name"]
        provider = agent_spec.get("provider", "local")
        user = agent_spec.get("user")
        resources = agent_spec.get("resources", {})
        ctrl_type = agent_spec.get("type", "agent")
        image = f"ventis-{agent_name.lower()}"
        runtime_id = f"ventis-{provider.lower()}-{agent_name.lower()}-{replica_index}"
        redis_host = _container_routing_host(host)

        if ensure_remote_image:
            self.controller._ensure_image_on_host(image, host, user)

        cmd = [
            "docker", "run", "-d", "-it",
            "--add-host=host.docker.internal:host-gateway",
            "--name", runtime_id,
            "-p", f"{host_port}:{self.CONTAINER_PORT}",
            "-e", f"VENTIS_AGENT_PORT={host_port}",
            "-e", f"VENTIS_AGENT_HOST={redis_host}",
            "-e", f"VENTIS_REDIS_HOST={redis_host}",
            "-e", f"VENTIS_REDIS_PORT={agent_spec.get('redis_port', 6379)}",
        ]
        if ctrl_type == "workflow":
            cmd.extend(["-p", f"{self.WORKFLOW_API_PORT}:8080"])
        if resources.get("cpu"):
            cmd.extend(["--cpus", str(resources["cpu"])] )
        if resources.get("memory"):
            cmd.extend(["--memory", f"{resources['memory']}m"])
        if resources.get("gpu"):
            cmd.extend(["--gpus", str(resources["gpu"])] )
        cmd.append(image)

        result = self.controller._run_cmd(cmd, host, user)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to launch {runtime_id}")

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
        if user:
            instance["user"] = user
        logger.info("Runtime ready: %s -> %s", runtime_id, instance["endpoint"])
        return instance

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
        result = self.controller._run_cmd(["docker", "rm", "-f", runtime_id], instance.get("host", DEFAULT_HOST), instance.get("user"))
        if result.returncode != 0:
            logger.warning("Failed to remove runtime %s", runtime_id)

    def _track_runtime(self, agent_name, runtime_id):
        containers = self.controller.containers.setdefault(agent_name, [])
        if runtime_id not in containers:
            containers.append(runtime_id)

    def _next_host_port(self, host):
        used = {
            int(instance["host_port"])
            for instance in self.list_instances()
            if instance.get("host") == host and instance.get("host_port")
        }
        port = DEFAULT_HOST_PORT_START
        while port in used:
            port += 1
        return port

    @staticmethod
    def _instance_id(provider, agent_name, replica_index):
        return f"{provider}:{agent_name}:{replica_index}"

    @classmethod
    def _instance_key(cls, provider, agent_name, replica_index):
        return f"agent_instance:{cls._instance_id(provider, agent_name, replica_index)}"

    def _instance_id_from_record(self, instance):
        return self._instance_id(instance["provider"], instance["agent_name"], int(instance["replica_index"]))

    def publish_routing_snapshot(self, agent_specs):
        services = {agent_spec["name"] for agent_spec in agent_specs}
        stateful = {agent_spec["name"] for agent_spec in agent_specs if agent_spec.get("stateful", False)}
        targets = list(getattr(self.controller, "node_redis", {}).values()) or [self.redis]

        for redis_client in targets:
            hdel = getattr(redis_client, "hdel", None) or redis_client.client.hdel
            existing_services = redis_client.smembers(self.SERVICES_SET_KEY)
            for stale in existing_services - services:
                redis_client.srem(self.SERVICES_SET_KEY, stale)
                hdel(self.ROUTING_STATEFUL_KEY, stale)
                hdel(self.ROUTING_ENDPOINTS_KEY, stale)
            for service in services:
                redis_client.sadd(self.SERVICES_SET_KEY, service)
                if service in stateful:
                    redis_client.hset(self.ROUTING_STATEFUL_KEY, service, "true")
                else:
                    hdel(self.ROUTING_STATEFUL_KEY, service)
                endpoints = [
                    self._routing_endpoint_for(item)
                    for item in sorted(self.list_instances(service), key=lambda item: int(item["replica_index"]))
                ]
                if endpoints:
                    redis_client.hset(self.ROUTING_ENDPOINTS_KEY, service, json.dumps(endpoints))
                else:
                    hdel(self.ROUTING_ENDPOINTS_KEY, service)

    def _routing_endpoint_for(self, instance):
        if instance.get("provider", "local").lower() == "local" and _is_local_host(instance.get("host")):
            return f"host.docker.internal:{instance['host_port']}"
        return instance["endpoint"]
