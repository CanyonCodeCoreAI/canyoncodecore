# Global Controller
# Daemon process that maintains a routing table in Redis for multiple local controllers.
# Periodically polls Redis to check controller health and updates the routing table.

import atexit
import logging
import shlex
import signal
import subprocess
import threading
import time
import json
import sys
import os
from concurrent.futures import ThreadPoolExecutor

import yaml
from ventis.controller.instance_manager import InstanceManager
from ventis.controller.utils.agent_specs import write_agent_specs
from ventis.controller.utils.env_file import resolve_env_file
from ventis.controller.utils.redis_utils import _wait_for_redis
from ventis.controller.utils.telemetry_logging import (
    assign_project_id,
    pull_runtime_information,
    send_runtime_information,
    send_agent_information,
)
from ventis.utils.redis_client import RedisClient
from ventis.utils.grpc_options import GRPC_CHANNEL_OPTIONS

# Add generated grpc_stubs from the local project to the path
sys.path.insert(0, os.path.abspath("grpc_stubs"))
import local_controler_pb2
import local_controler_pb2_grpc
import grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _is_local_host(host):
    return host in {"localhost", "127.0.0.1"}


def _container_routing_host(host):
    return "host.docker.internal" if _is_local_host(host) else host


class GlobalController(object):
    """
    Daemon that manages a routing table across multiple local controller instances.

    At startup it reads a YAML config file listing known agents, writes the
    initial routing table to Redis, then enters a polling loop that periodically
    checks controller health and refreshes the table.

    Designed to be subclassed — override the _on_* hooks to extend behavior.
    """

    ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
    ROUTING_STATEFUL_KEY = "routing_table:stateful"
    SERVICES_SET_KEY = "routing_table:services"
    POLICY_RULES_KEY = "policy:rules"
    IDENTITY_KEY = "controller:identity" # has controllers current project_id and database_url

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load_config(config_path)
        # Validate before launching anything: an agent that boots without its
        # API keys fails deep inside a container, where it is expensive to debug.
        self.env_file_path = resolve_env_file(self.config)

        redis_cfg = self.config.get("redis", {})
        self.redis = RedisClient(
            host=redis_cfg.get("host", "localhost"),
            port=redis_cfg.get("port", 6379),
            db=redis_cfg.get("db", 0),
        )

        self.poll_interval = self.config.get("poll_interval", 5)
        self.cleanup_interval = self.config.get("cleanup_interval", 10)
        self.controllers = self.config.get("agents", [])
        self.running = False
        self.containers = {}  # name -> [container_name, ...]
        self.redis_containers = {}  # host -> container_name
        self.node_redis = {}  # host -> RedisClient
        self._last_status = {}  # (host, port) -> last known status
        self._last_metrics_poll_time = {}  # (host, port) -> time.time() of last metrics read
        self._lc_stubs = {}  # endpoint -> gRPC stub
        self.instance_manager = InstanceManager(self)
        assign_project_id(self.config.get("project_id",0))

        # Clean up any stale containers from previous runs
        self._cleanup_stale_containers()

        # Launch Redis on each unique node, then write routing table and policies
        self._launch_redis_containers()
        write_agent_specs(self.config_path, self.redis)
        self._write_resource_specs()
        self._load_and_write_policies()
        self._write_identity()
        self.instance_manager.publish_routing_snapshot(self.controllers)
        logger.info(
            "Global controller initialized with %d controller(s).",
            len(self.controllers),
        )

        # Start background cleanup thread
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    # ------------------------------------------------------------------ #
    #  Stale container cleanup                                             #
    # ------------------------------------------------------------------ #

    def _cleanup_stale_containers(self):
        """Remove any containers from previous runs before launching new ones."""
        logger.info("Checking for stale containers from previous runs...")

        # Collect all expected container names and the hosts they run on
        # { host: (user, [container_names]) }
        host_containers = {}

        for ctrl in self.controllers:
            user = ctrl.get("user")
            placements = self._get_replica_placements(ctrl)
            name = ctrl["name"]

            for i, (host, port) in enumerate(placements):
                if host not in host_containers:
                    host_containers[host] = (user, set())
                host_containers[host][1].add(f"ventis-redis-{host.replace('.', '-')}")
                host_containers[host][1].add(f"ventis-{name.lower()}-{i}")

        # Try to remove each one on its respective host
        for host, (user, container_names) in host_containers.items():
            for container_name in container_names:
                try:
                    inspect = self._run_cmd(
                        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                        host,
                        user,
                    )
                    if inspect.returncode == 0 and inspect.stdout.strip() == "true":
                        continue  # already running -- a live replica, not stale
                    self._run_cmd(["docker", "rm", "-f", container_name], host, user)
                except Exception:
                    pass  # Container didn't exist, that's fine

        logger.info("Stale container cleanup complete.")

    # ------------------------------------------------------------------ #
    #  Config                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_config(config_path):
        """Load the YAML config file."""
        with open(config_path, "r") as f:
            return yaml.safe_load(f)

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

    def reload_config(self):
        """Reload the config file and rebuild the routing table."""
        logger.info("Reloading config from %s", self.config_path)
        self.config = self._load_config(self.config_path)
        self.env_file_path = resolve_env_file(self.config)
        self.controllers = self.config.get("agents", [])
        self.poll_interval = self.config.get("poll_interval", 5)
        assign_project_id(self.config.get("project_id", 0))
        self._write_identity()
        self.instance_manager.publish_routing_snapshot(self.controllers)

    def _write_resource_specs(self):
        """Write the per-agent resource specs to Redis."""
        for ctrl in self.controllers:
            name = ctrl["name"]
            resources = ctrl.get("resources", {})
            self.redis.hset_multiple(
                f"agent:{name}:resources",
                {
                    "cpu": str(resources.get("cpu", 1)),
                    "memory": str(resources.get("memory", 512)),
                    "replicas": str(int(ctrl.get("replicas", 1))),
                },
            )

    def _load_policy_rules(self):
        """Load policy rules from config/policy.yaml."""
        config_dir = os.path.dirname(os.path.abspath(self.config_path))
        policy_path = os.path.join(config_dir, "policy.yaml")

        if not os.path.isfile(policy_path):
            logger.info(
                "No policy file found at %s, skipping policy setup.", policy_path
            )
            return []

        with open(policy_path, "r") as f:
            policy_config = yaml.safe_load(f)

        rules = policy_config.get("rules", [])

        # Sort rules by specificity: most match keys first
        # This way the local controller can iterate and use the first matching rule.
        rules.sort(key=lambda r: len(r.get("match", {})), reverse=True)
        return rules

    def _load_and_write_policies(self):
        """Load policy rules and publish them to every host Redis."""
        rules = self._load_policy_rules()
        targets = list(self.node_redis.values()) or [self.redis]
        rules_json = json.dumps(rules)
        for redis_client in targets:
            redis_client.set("policy:rules", rules_json)

        logger.info(
            "Policy rules written to %d Redis instance(s): %d rule(s)",
            len(targets),
            len(rules),
        )

    # Only relevant for demo purposes
    def _write_identity(self):
        """Publish the current project/database identity to every node's Redis."""
        payload = {
            "project_id": str(self.config.get("project_id", 0)),
            "database_url": self.config.get("database", {}).get("url") or "",
        }
        targets = list(self.node_redis.values()) or [self.redis]
        for redis_client in targets:
            redis_client.hset_multiple(self.IDENTITY_KEY, payload)

        logger.info("Identity (project %s) published to %d Redis instance(s).", payload["project_id"], len(targets))

    # Routing reads are direct Redis calls now that InstanceManager owns publication:
    # - self.redis.hgetall(self.ROUTING_ENDPOINTS_KEY)
    # - self.redis.hget(self.ROUTING_ENDPOINTS_KEY, service_name)

    def get_node_redis(self, host):
        """Get the RedisClient for a specific node."""
        return self.node_redis.get(host)

    # ------------------------------------------------------------------ #
    #  Redis container management                                         #
    # ------------------------------------------------------------------ #

    def _redis_container_healthy(self, container_name, host, user, connect_host, redis_port):
        """Check whether an existing Redis container is already up and answering."""
        inspect = self._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name], host, user
        )
        if inspect.returncode != 0 or inspect.stdout.strip() != "true":
            return False
        try:
            probe = RedisClient(host=connect_host, port=redis_port)
            _wait_for_redis(probe, host, redis_port, timeout=5, interval=1)
            return True
        except TimeoutError:
            return False

    def _launch_redis_containers(self):
        """Launch a Redis container on each unique node, reusing one that's already healthy."""
        # Collect unique nodes from all replica placements
        nodes = {}
        for ctrl in self.controllers:
            user = ctrl.get("user")
            redis_port = ctrl.get("redis_port", 6379)
            for host, _port in self._get_replica_placements(ctrl):
                if host not in nodes:
                    nodes[host] = {
                        "user": user,
                        "redis_port": redis_port,
                    }

        for host, node_cfg in nodes.items():
            redis_port = node_cfg["redis_port"]
            user = node_cfg["user"]
            container_name = f"ventis-redis-{host.replace('.', '-')}"
            # For localhost, connect directly; for remote, connect via host IP
            connect_host = "localhost" if host in ("localhost", "127.0.0.1") else host

            if self._redis_container_healthy(container_name, host, user, connect_host, redis_port):
                logger.info("Reusing existing Redis container %s on %s", container_name, host)
                self.redis_containers[host] = container_name
            else:
                cmd = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container_name,
                    "-p",
                    f"{redis_port}:6379",
                    "redis:alpine",
                ]

                try:
                    result = self._run_cmd(cmd, host, user)
                    if result.returncode == 0:
                        self.redis_containers[host] = container_name
                        logger.info(
                            "Launched Redis container %s on %s:%d",
                            container_name,
                            host,
                            redis_port,
                        )
                    else:
                        logger.critical(
                            "Failed to launch Redis on %s: %s",
                            host,
                            result.stderr.strip(),
                        )
                        sys.exit(1)
                except FileNotFoundError:
                    logger.critical(
                        "Docker is not installed or not in PATH. Cannot launch Redis."
                    )
                    sys.exit(1)
                except Exception as e:
                    logger.critical("Failed to launch Redis on %s: %s", host, e)
                    sys.exit(1)

            # Create a RedisClient for this node
            redis_client = RedisClient(host=connect_host, port=redis_port)
            _wait_for_redis(redis_client, host, redis_port)
            self.node_redis[host] = redis_client

        # Update the primary redis client to the local node's Redis
        if "localhost" in self.node_redis:
            self.redis = self.node_redis["localhost"]

        logger.info("Redis launched on %d node(s).", len(self.redis_containers))

    def _stop_redis_containers(self):
        """Stop and remove all launched Redis containers."""
        nodes = {}
        for ctrl in self.controllers:
            if ctrl.get("provider", "local").upper() == "EC2":
                continue
            user = ctrl.get("user")
            redis_port = ctrl.get("redis_port", 6379)
            for host, _port in self._get_replica_placements(ctrl):
                nodes.setdefault(host, {"user": user, "redis_port": redis_port})
        for host, container_name in self.redis_containers.items():
            user = nodes.get(host, {}).get("user")
            try:
                self._run_cmd(["docker", "stop", container_name], host, user)
                self._run_cmd(["docker", "rm", container_name], host, user)
                logger.info("Stopped Redis %s on %s", container_name, host)
            except Exception as e:
                logger.warning("Failed to stop Redis %s: %s", container_name, e)

        self.redis_containers.clear()
        self.node_redis.clear()

    # ------------------------------------------------------------------ #
    #  Startup health check                                               #
    # ------------------------------------------------------------------ #

    def _get_node_redis_for(self, host):
        """Get the Redis client for a given host, falling back to self.redis."""
        return self.node_redis.get(host, self.redis)

    def _agent_host_key(self, host):
        """Return the host string as seen by Docker containers (for status key matching)."""
        return _container_routing_host(host)

    def _wait_for_healthy(self, timeout=30, interval=2):
        """
        Block until all controllers report healthy in Redis, or until timeout.

        Args:
            timeout:  Maximum seconds to wait.
            interval: Seconds between checks.
        """
        deadline = time.time() + timeout
        pending = [
            (instance["agent_name"], instance["host"], instance["host_port"])
            for instance in self.instance_manager.list_instances()
        ]

        logger.info(
            "Waiting for %d replica(s) to become healthy (timeout=%ds)...",
            len(pending),
            timeout,
        )

        while pending and time.time() < deadline:
            still_pending = []
            for name, host, port in pending:
                node_redis = self._get_node_redis_for(host)
                agent_host = self._agent_host_key(host)
                status = node_redis.get(f"controller:{agent_host}:{port}:status")
                if status == "healthy":
                    logger.info("Controller %s (%s:%s) is ready.", name, host, port)
                    self._last_status[(host, port)] = "healthy"
                else:
                    still_pending.append((name, host, port))
            pending = still_pending
            if pending:
                time.sleep(interval)

        if pending:
            for name, host, port in pending:
                logger.warning(
                    "Controller %s (%s:%s) not ready after %ds.",
                    name,
                    host,
                    port,
                    timeout,
                )

    # ------------------------------------------------------------------ #
    #  Polling loop                                                       #
    # ------------------------------------------------------------------ #

    def run(self):
        """Start the daemon polling loop."""
        self.running = True
        logger.info(
            "Global controller started, polling every %ds...", self.poll_interval
        )
        try:
            while self.running:
                try:
                    self._poll_controllers()
                except Exception as e:
                    logger.warning("Polling loop encountered an error: %s", e)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.stop()

    def _poll_controllers(self):
        """
        Check the health of each registered controller replica via its node's Redis.
        Also retrieves the request calls made in each instance.
        """
        for instance in self.instance_manager.list_instances():
            name = instance["agent_name"]
            host = instance["host"]
            port = instance["host_port"]
            node_redis = self._get_node_redis_for(host)
            try:
                send_runtime_information(
                    pull_runtime_information(node_redis),
                    node_redis,
                    self.config.get("database", {}).get("url"),
                )
            except Exception as e:
                logger.warning(
                    "Failed to write runtime information for instance %s (%s:%s) "
                    "(non-fatal): %s",
                    name,
                    host,
                    port,
                    e,
                )
            agent_host = self._agent_host_key(host)
            status_key = f"controller:{agent_host}:{port}:status"
            metrics_key = f"controller:{agent_host}:{port}:metrics"

            # Getting metrics from local controllers
            # See LocalController._execute_locally
            try:
                metrics = node_redis.hgetall(metrics_key)
                if metrics:
                    now = time.time()
                    requests_served = int(float(metrics.get("requests_served") or 0))
                    elapsed = now - self._last_metrics_poll_time.get(
                        (host, port), now - self.poll_interval
                    )
                    throughput = requests_served / elapsed if elapsed > 0 else 0.0
                    self._last_metrics_poll_time[(host, port)] = now

                    try:
                        send_agent_information(
                            [
                                {
                                    **instance,
                                    **metrics,
                                    "requests_served": requests_served,
                                    "throughput": throughput,
                                }
                            ],
                            self.config.get("database", {}).get("url"),
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to write agent information for instance %s (%s:%s) "
                            "(non-fatal): %s",
                            name,
                            host,
                            port,
                            e,
                        )
                    else:
                        # Only clear the accumulated counters once they've actually been persisted
                        node_redis.hset_multiple(
                            metrics_key,
                            {"full_failures": 0, "error_count": 0, "requests_served": 0},
                        )
            except Exception as e:
                logger.warning(
                    "Failed to poll metrics for instance %s (%s:%s): %s",
                    name,
                    host,
                    port,
                    e,
                )

            try:
                status = node_redis.get(status_key) or "unknown"
                prev = self._last_status.get((host, port))

                if status != prev:
                    if status == "healthy":
                        logger.info(
                            "Controller %s (%s:%s) is now healthy.", name, host, port
                        )
                        self._on_controller_healthy(name, host, port)
                    else:
                        logger.warning(
                            "Controller %s (%s:%s) status changed: %s -> %s",
                            name,
                            host,
                            port,
                            prev or "(none)",
                            status,
                        )
                        self._on_controller_unhealthy(name, host, port)
                    self._last_status[(host, port)] = status
                else:
                    # No change — healthy stays quiet, unhealthy stays quiet too
                    if status == "healthy":
                        self._on_controller_healthy(name, host, port)
                    else:
                        self._on_controller_unhealthy(name, host, port)
            except Exception as e:
                logger.warning(
                    "Failed to poll status for instance %s (%s:%s): %s",
                    name,
                    host,
                    port,
                    e,
                )

    # ------------------------------------------------------------------ #
    #  Extensibility hooks — override in subclasses                       #
    # ------------------------------------------------------------------ #

    def _on_controller_healthy(self, name, host, port):
        """Called when a controller is detected as healthy."""
        pass

    def _on_controller_unhealthy(self, name, host, port):
        """Called when a controller is unreachable or unhealthy."""
        pass

    def _on_routing_table_updated(self, table):
        """Called after the routing table has been written to Redis."""
        pass

    # ------------------------------------------------------------------ #
    #  Cleanup trigger                                                     #
    # ------------------------------------------------------------------ #

    def _get_lc_stub(self, endpoint):
        """Get or create a cached gRPC stub for a local controller endpoint."""
        if endpoint not in self._lc_stubs:
            channel = grpc.insecure_channel(endpoint, options=GRPC_CHANNEL_OPTIONS)
            self._lc_stubs[endpoint] = local_controler_pb2_grpc.LocalControllerStub(
                channel
            )
        return self._lc_stubs[endpoint]

    def _cleanup_loop(self):
        """Background thread: periodically trigger cleanup of completed requests."""
        while True:
            time.sleep(self.cleanup_interval)
            try:
                self._trigger_cleanup()
            except Exception as e:
                logger.warning("Cleanup loop encountered an error: %s", e)

    def _trigger_cleanup(self):
        """Broadcast a batched Cleanup gRPC to all instances for every completed request, gathered from every node's Redis."""
        # Falls back to self.redis alone if node_redis is unset/empty.
        node_redis_map = getattr(self, "node_redis", None) or {}
        redis_clients = list(node_redis_map.values()) or [self.redis]

        completed_by_client = {}
        all_completed = set()
        for client in redis_clients:
            completed = client.smembers("request:completed")
            if completed:
                completed_by_client[client] = completed
                all_completed.update(completed)

        if not all_completed:
            return

        payload = json.dumps({"request_ids": list(all_completed)})

        def _send(instance):
            endpoint = instance["endpoint"]
            try:
                stub = self._get_lc_stub(endpoint)
                stub.Cleanup(local_controler_pb2.JsonResponse(resonse=payload))
                logger.debug(
                    "Sent Cleanup batch of %d request(s) to %s",
                    len(all_completed),
                    endpoint,
                )
            except Exception as e:
                logger.warning("Failed to trigger cleanup on %s: %s", endpoint, e)

        instances = self.instance_manager.list_instances()
        if instances:
            with ThreadPoolExecutor(max_workers=len(instances)) as executor:
                list(executor.map(_send, instances))

        logger.info(
            "Triggered cleanup for %d completed request(s) across %d node(s)",
            len(all_completed),
            len(completed_by_client),
        )
        # Drain each node's own set from the same client it was read from.
        for client, completed in completed_by_client.items():
            client.srem("request:completed", *completed)

    # ------------------------------------------------------------------ #
    #  Runtime launching                                                  #
    # ------------------------------------------------------------------ #

    def _ssh_args(self, host, user=None):
        """Return the `ssh ... target` prefix used to reach a remote host."""
        ssh_key_path = os.path.expanduser(
            self.config.get("ec2", {}).get("ssh_private_key_path", "~/.ssh/ventis_ec2")
        )
        return [
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
            f"{user}@{host}" if user else host,
        ]

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

        remote_cmd = " ".join(cmd)
        if cmd and cmd[0] == "docker":
            remote_cmd = f"sudo {remote_cmd}"
        return subprocess.run(
            self._ssh_args(host, user) + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=180,
        )

    def _push_file(self, local_path, remote_path, host, user=None):
        """
        Copy a local file to a remote host over SSH.

        Streams the bytes through `cat` under `umask 077` rather than using
        `scp`, so a secrets file is never briefly world-readable on the far
        side.

        Anything already sitting at the destination is removed first: `umask`
        only governs files the shell creates, and `>` follows symlinks. Without
        the `rm`, a local user on the remote host could pre-create the path
        world-readable, or point it at a file of their own, and collect
        whatever we write there.

        Returns:
            subprocess.CompletedProcess
        """
        quoted = shlex.quote(remote_path)
        remote_cmd = f"umask 077; rm -f {quoted}; cat > {quoted}"
        with open(local_path, "rb") as f:
            result = subprocess.run(
                self._ssh_args(host, user) + [remote_cmd],
                stdin=f,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to copy {local_path} to {host}:{remote_path}: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        return result

    def launch_docker_agents(self):
        """Launch all configured runtimes through InstanceManager."""
        try:
            instances = self.instance_manager.ensure_instances(self.controllers)
        except FileNotFoundError:
            logger.critical(
                "Docker is not installed or not in PATH. Cannot launch agents."
            )
            self._stop_redis_containers()
            sys.exit(1)
        except Exception as e:
            logger.critical("Failed to launch configured runtimes: %s", e)
            self._stop_docker_agents()
            self._stop_redis_containers()
            sys.exit(1)

        logger.info(
            "Launched %d Docker container(s) across %d service(s).",
            len(instances),
            len({instance["agent_name"] for instance in instances}),
        )

    def _stop_docker_agents(self):
        """Stop and remove all launched runtimes."""
        for instance in self.instance_manager.list_instances():
            self.instance_manager.remove_instance(
                self.instance_manager._instance_id_from_record(instance)
            )

        self.containers.clear()
        logger.info("All Docker containers stopped.")

    # ------------------------------------------------------------------ #
    #  Shutdown                                                           #
    # ------------------------------------------------------------------ #

    def cleanup(self):
        """Full cleanup — stop all containers and Redis, called on exit."""
        if not self.running and not self.containers and not self.redis_containers:
            return  # Already cleaned up
        logger.info("Cleaning up all resources...")
        self.stop()

    def stop(self):
        """Gracefully shut down the daemon and all agent processes."""
        self.running = False
        self._stop_docker_agents()
        self._stop_redis_containers()
        logger.info("Global controller shut down.")


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.join(script_dir, "..", "..")
    default_config = os.path.join(project_root, "config", "global_controller.yaml")

    import argparse

    parser = argparse.ArgumentParser(description="Ventis Global Controller daemon.")
    parser.add_argument(
        "-c",
        "--config",
        default=default_config,
        help="Path to the YAML config file (default: config/global_controller.yaml)",
    )
    args = parser.parse_args()

    controller = GlobalController(args.config)

    # Register cleanup on Ctrl+C (SIGINT) and kill (SIGTERM)
    def _signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        controller.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Register config reload on SIGHUP and reload
    def _reload_handler(sig, frame):
        logger.info("Received SIGHUP, reloading config...")
        try:
            controller.reload_config()
        except Exception as e:
            logger.error("Reload failed: %s", e)

    signal.signal(signal.SIGHUP, _reload_handler)
    atexit.register(controller.cleanup)

    controller.launch_docker_agents()
    controller._wait_for_healthy()
    controller.run()
