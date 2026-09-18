# Controller Context
# Config, Redis clients and command execution: the controller surface that
# InstanceManager and the cloud provider runtimes depend on.
# GlobalController subclasses this; the reconciler process builds one directly.

import logging
import os
import subprocess

import yaml

from canyonos_core.controller.utils.redis_client import RedisClient

logger = logging.getLogger(__name__)


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def _container_routing_host(host):
    return "host.docker.internal" if _is_local_host(host) else host


def _redis_connect_host(host):
    """Host to open a Redis connection to from this process."""
    return "localhost" if _is_local_host(host) else host


class ControllerContext(object):
    """
    Everything InstanceManager and the provider runtimes read off `controller`,
    with no cluster bootstrap and no gRPC dependency.

    Bootstrap (stale container cleanup, launching Redis, publishing the routing
    snapshot) belongs to GlobalController alone -- a second process building this
    context must be able to provision instances without repeating any of it.
    """

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load_config(config_path)

        redis_cfg = self.config.get("redis", {})
        self.redis = RedisClient(
            host=redis_cfg.get("host", "localhost"),
            port=redis_cfg.get("port", 6379),
            db=redis_cfg.get("db", 0),
        )

        self.poll_interval = self.config.get("poll_interval", 5)
        self._set_controllers(self.config.get("agents", []))
        self.containers = {}  # name -> [runtime_id, ...]
        self.redis_containers = {}  # host -> container_name
        self.node_redis = {}  # host -> RedisClient

    # ------------------------------------------------------------------ #
    #  Config                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_config(config_path):
        """Load the YAML config file."""
        with open(config_path, "r") as f:
            return yaml.safe_load(f)

    def _set_controllers(self, agents):
        """Set the agent spec list and its by-name index together so they can't drift."""
        self.controllers = agents
        self.agent_specs = {spec["name"]: spec for spec in agents}

    @staticmethod
    def _get_replica_placements(ctrl):
        """Normalize replicas into a list of (host, port) placements."""
        replicas = ctrl.get("replicas", 1)
        default_host = ctrl.get("host", "localhost")
        base_port = ctrl.get("port", 50051)

        if isinstance(replicas, int):
            return [(default_host, base_port + i) for i in range(replicas)]
        if isinstance(replicas, list):
            return [
                (r.get("host", default_host), r.get("port", base_port))
                for r in replicas
            ]
        return [(default_host, base_port)]

    # ------------------------------------------------------------------ #
    #  Redis clients                                                      #
    # ------------------------------------------------------------------ #

    def _get_node_redis_for(self, host):
        """Get the Redis client for a given host, falling back to self.redis."""
        return self.node_redis.get(host, self.redis)

    def _localhost_redis_port(self):
        """The Redis port the local node's container was published on, if any."""
        for ctrl in self.controllers:
            for host, _port in self._get_replica_placements(ctrl):
                if _is_local_host(host):
                    return int(ctrl.get("redis_port", 6379))
        return None

    def attach_local_node_redis(self):
        """
        Point self.redis at the local node's Redis without launching anything.

        GlobalController repoints its primary client at node_redis["localhost"]
        after launching that container; a process that only attaches to a running
        cluster has to reach the same client or it reads a different Redis.
        """
        port = self._localhost_redis_port()
        if port is None:
            return
        client = RedisClient(host="localhost", port=port)
        self.node_redis["localhost"] = client
        self.redis = client

    def node_redis_for_instance(self, instance):
        """
        Redis client for the node an instance runs on, connecting on demand.

        An instance provisioned by another process registered its node's Redis in
        that process's node_redis only, so this connects from the instance's own
        record rather than assuming this process launched it.
        """
        host = instance.get("host")
        if not host:
            return self.redis
        if host in self.node_redis:
            return self.node_redis[host]

        port = int(instance.get("redis_port") or 6379)
        client = RedisClient(host=_redis_connect_host(host), port=port)
        self.node_redis[host] = client
        return client

    def _agent_host_key(self, host):
        """Return the host string as seen by Docker containers (for status key matching)."""
        return _container_routing_host(host)

    # ------------------------------------------------------------------ #
    #  Command execution                                                  #
    # ------------------------------------------------------------------ #

    def _run_cmd(self, cmd, host, user=None):
        """
        Run a command locally or on a remote host via SSH.

        Args:
            cmd:  Command list to run.
            host: Target host.
            user: SSH user for remote hosts (None for localhost).

        Returns:
            subprocess.CompletedProcess
        """
        is_local = _is_local_host(host)
        if is_local:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        else:
            ssh_key_path = os.path.expanduser(
                self.config.get("ec2", {}).get(
                    "ssh_private_key_path", "~/.ssh/ventis_ec2"
                )
            )
            ssh_target = f"{user}@{host}" if user else host
            remote_cmd = " ".join(cmd)
            if cmd and cmd[0] == "docker":
                remote_cmd = f"sudo {remote_cmd}"
            return subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "ServerAliveInterval=10",
                    "-o",
                    "ServerAliveCountMax=3",
                    "-i",
                    ssh_key_path,
                    ssh_target,
                    remote_cmd,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
