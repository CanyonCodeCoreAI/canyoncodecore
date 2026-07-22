"""
Coordinate agent runtime instances for the controller.

This file decides whether each agent replica should run locally or on EC2,
starts missing instances, records their runtime metadata in Redis, and
publishes the routing data other parts of Ventis use to reach those agents.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from ventis.controller.cloud_provider_logic.Local import _runtime as local_runtime

DEFAULT_HOST_PORT_START = 8000


class InstanceManager:
    ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
    ROUTING_STATEFUL_KEY = "routing_table:stateful"
    SERVICES_SET_KEY = "routing_table:services"

    def __init__(self, controller, redis_client=None):
        self.controller = controller
        self._redis = redis_client

    @property
    def redis(self):
        return self._redis or self.controller.redis

    def ensure_instances(self, agent_specs):
        self._agent_specs = list(agent_specs)
        instances = []
        existing = []
        jobs = []

        for agent_spec in self._agent_specs:
            agent_name = agent_spec["name"]
            provider = agent_spec.get("provider", "local")
            runtime = self._provider_runtime(provider)
            self.controller.containers.setdefault(agent_name, [])

            validate = getattr(runtime, "validate_config", None)
            if validate:
                validate()

            for replica_index in range(int(agent_spec.get("replicas", 1))):
                instance_id = self._instance_id(provider, agent_name, replica_index)
                key = self._instance_key(provider, agent_name, replica_index)
                instance = self.redis.hgetall(key)

                if instance and instance.get("runtime_id"):
                    existing.append((agent_name, instance_id, instance))
                    continue

                reserved_port = None
                if provider == "local":
                    host = agent_spec.get("host", local_runtime.DEFAULT_HOST)
                    reserved_port = self._next_host_port(
                        host, key, agent_name, provider, replica_index
                    )

                jobs.append(
                    {
                        "agent_name": agent_name,
                        "agent_spec": agent_spec,
                        "runtime": runtime,
                        "replica_index": replica_index,
                        "instance_id": instance_id,
                        "reserved_port": reserved_port,
                    }
                )

        max_workers = min(len(jobs),os.cpu_count()*100)
        provisioned = []
        if jobs:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_job = {
                    executor.submit(self._provision_one, job): job for job in jobs
                }
                for future in as_completed(future_to_job):
                    job = future_to_job[future]
                    instance = future.result()
                    provisioned.append(
                        (job["agent_name"], job["instance_id"], instance)
                    )

        for agent_name, instance_id, instance in existing + provisioned:
            self._add_instance_to_agent(agent_name, instance_id)
            self._track_runtime(agent_name, instance["runtime_id"])
            instances.append(instance)

        self.publish_routing_snapshot(self._agent_specs)
        return instances

    def _provision_one(self, job):
        runtime = job["runtime"]
        agent_spec = job["agent_spec"]
        replica_index = job["replica_index"]
        reserved_port = job["reserved_port"]

        next_host_port = lambda host: reserved_port

        provisioned = runtime.provision_instance(
            agent_spec, replica_index, next_host_port
        )
        instance = runtime.bootstrap_instance(provisioned, agent_spec, replica_index)
        self._write_instance(instance)
        return instance

    def _write_instance(self, instance):
        key = self._instance_key(
            instance["provider"], instance["agent_name"], int(instance["replica_index"])
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
        self.publish_routing_snapshot(
            getattr(self, "_agent_specs", getattr(self.controller, "controllers", []))
        )

    def list_instances(self, agent_name=None):
        if agent_name:
            instance_ids = sorted(self.redis.smembers(f"agent:{agent_name}:instances"))
            return [
                instance
                for instance_id in instance_ids
                if (instance := self.redis.hgetall(f"agent_instance:{instance_id}"))
            ]

        return [
            instance
            for key in sorted(self.redis.scan_keys("agent_instance:*"))
            if (instance := self.redis.hgetall(key))
        ]

    def _destroy_runtime(self, instance):
        runtime = self._provider_runtime(instance.get("provider", "local"))
        runtime.terminate_instance(instance)

    def _track_runtime(self, agent_name, runtime_id):
        containers = self.controller.containers.setdefault(agent_name, [])
        if runtime_id not in containers:
            containers.append(runtime_id)

    def _next_host_port(self, host, key, agent_name, provider, replica_index):
        used = {
            int(instance["host_port"])
            for instance in self.list_instances()
            if instance.get("host") == host and instance.get("host_port")
        }
        port = DEFAULT_HOST_PORT_START
        while port in used:
            port += 1

        self.redis.hset_multiple(
            key,
            {
                "agent_name": agent_name,
                "provider": provider,
                "replica_index": str(replica_index),
                "host": host,
                "host_port": str(port),
            },
        )
        return port

    @staticmethod
    def _instance_id(provider, agent_name, replica_index):
        return f"{provider}:{agent_name}:{replica_index}"

    @classmethod
    def _instance_key(cls, provider, agent_name, replica_index):
        return f"agent_instance:{cls._instance_id(provider, agent_name, replica_index)}"

    def _instance_id_from_record(self, instance):
        return self._instance_id(
            instance["provider"], instance["agent_name"], int(instance["replica_index"])
        )

    def _provider_runtime(self, provider):
        if provider.upper() == "EC2":
            from ventis.controller.cloud_provider_logic.EC2 import _runtime as runtime
        else:
            runtime = local_runtime
        runtime._controller = self.controller
        return runtime

    def publish_routing_snapshot(self, agent_specs):
        services = {agent_spec["name"] for agent_spec in agent_specs}
        stateful = {
            agent_spec["name"]
            for agent_spec in agent_specs
            if agent_spec.get("stateful", False)
        }
        targets = list(getattr(self.controller, "node_redis", {}).values()) or [
            self.redis
        ]

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
                    for item in sorted(
                        self.list_instances(service),
                        key=lambda item: int(item["replica_index"]),
                    )
                ]
                if endpoints:
                    redis_client.hset(
                        self.ROUTING_ENDPOINTS_KEY, service, json.dumps(endpoints)
                    )
                else:
                    hdel(self.ROUTING_ENDPOINTS_KEY, service)

    def _routing_endpoint_for(self, instance):
        runtime = self._provider_runtime(instance.get("provider", "local"))
        return runtime.routing_endpoint_for(instance)
