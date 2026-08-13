# Global Controller
# Daemon process that maintains a routing table in Redis for multiple local controllers.
# Periodically polls Redis to check controller health and updates the routing table.

import atexit
import logging
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
from ventis.controller.telemetry_poller import TelemetryPoller
from ventis.controller.utils.agent_specs import write_agent_specs
from ventis.controller.utils.redis_utils import _wait_for_redis
from ventis.controller.utils.telemetry_logging import assign_project_id
from ventis.utils.redis_client import RedisClient

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
    initial routing table to Redis and starts its cleanup worker. ``run()``
    starts telemetry before entering the controller health loop.

    Designed to be subclassed — override the _on_* hooks to extend behavior.
    """

    ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
    ROUTING_STATEFUL_KEY = "routing_table:stateful"
    SERVICES_SET_KEY = "routing_table:services"
    POLICY_RULES_KEY = "policy:rules"
    IDENTITY_KEY = "controller:identity"

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
        self.cleanup_interval = self.config.get("cleanup_interval", 10)
        self.controllers = self.config.get("agents", [])
        self.running = False
        self.containers = {}  # name -> [container_name, ...]
        self.redis_containers = {}  # host -> container_name
        self.node_redis = {}  # host -> RedisClient
        self._last_status = {}  # (host, port) -> last known status
        self._lc_stubs = {}  # endpoint -> gRPC stub
        self.instance_manager = InstanceManager(self)
        self._shutdown_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._run_thread = None
        self._cleanup_thread = None
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
        self.telemetry_poller = TelemetryPoller(
            poll_interval=self.poll_interval,
            database_url=self.config.get("database", {}).get("url") or "",
        )
        logger.info(
            "Global controller initialized with %d controller(s).",
            len(self.controllers),
        )

        # Start background cleanup thread
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="ventis-controller-cleanup",
            daemon=True,
        )
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
        self.controllers = self.config.get("agents", [])
        self.poll_interval = self.config.get("poll_interval", 5)
        assign_project_id(self.config.get("project_id", 0))
        telemetry_poller = getattr(self, "telemetry_poller", None)
        if telemetry_poller is not None:
            telemetry_poller.update_settings(
                self.poll_interval,
                self.config.get("database", {}).get("url") or "",
            )
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
            return

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
    #  Background workers                                                 #
    # ------------------------------------------------------------------ #

    def run(self):
        """Run the controller health loop while telemetry polls in the background."""
        with self._lifecycle_lock:
            if self.running:
                return
            self.running = True
            self._shutdown_event.clear()
            self._run_thread = threading.current_thread()

            self.telemetry_poller.start()

        logger.info(
            "Global controller started (poll interval %ds).",
            self.poll_interval,
        )
        try:
            self._health_monitor_loop()
        except KeyboardInterrupt:
            self.stop()
        finally:
            with self._lifecycle_lock:
                if self._run_thread is threading.current_thread():
                    self._run_thread = None

    def _poll_controller_health(self):
        """Check each registered controller replica's health in Redis."""
        instances = self.instance_manager.list_instances()
        targets = [
            (instance, self._get_node_redis_for(instance["host"]))
            for instance in instances
        ]
        self.telemetry_poller.request_poll(targets)
        for instance, node_redis in targets:
            try:
                self._poll_instance_health(instance, node_redis)
            except Exception as e:
                logger.warning(
                    "Failed to poll health for instance %s (%s:%s) (non-fatal): %s",
                    instance.get("agent_name", "(unknown)"),
                    instance.get("host", "(unknown)"),
                    instance.get("host_port", "(unknown)"),
                    e,
                )

    def _poll_instance_health(self, instance, node_redis=None):
        """Read one instance's status and dispatch its health hook."""
        name = instance["agent_name"]
        host = instance["host"]
        port = instance["host_port"]
        if node_redis is None:
            node_redis = self._get_node_redis_for(host)
        agent_host = self._agent_host_key(host)
        status_key = f"controller:{agent_host}:{port}:status"
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

    def _health_monitor_loop(self):
        """Poll controller health independently from telemetry persistence."""
        while not self._shutdown_event.is_set():
            try:
                self._poll_controller_health()
            except Exception as e:
                logger.warning("Health monitor encountered an error: %s", e)
            if self._shutdown_event.wait(self.poll_interval):
                break

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
            channel = grpc.insecure_channel(endpoint)
            self._lc_stubs[endpoint] = local_controler_pb2_grpc.LocalControllerStub(
                channel
            )
        return self._lc_stubs[endpoint]

    def _cleanup_loop(self):
        """Background thread: periodically trigger cleanup of completed requests."""
        while not self._shutdown_event.wait(self.cleanup_interval):
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

        ready = {
            request_id
            for request_id in all_completed
            if self._request_telemetry_persisted(request_id, redis_clients)
        }
        if not ready:
            return

        payload = json.dumps({"request_ids": list(ready)})

        def _send(instance):
            endpoint = instance["endpoint"]
            try:
                stub = self._get_lc_stub(endpoint)
                stub.Cleanup(local_controler_pb2.JsonResponse(resonse=payload))
                logger.debug(
                    "Sent Cleanup batch of %d request(s) to %s",
                    len(ready),
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
            len(ready),
            len(completed_by_client),
        )
        # Drain each node's own set from the same client it was read from.
        for client, completed in completed_by_client.items():
            completed_ready = completed.intersection(ready)
            if completed_ready:
                client.srem("request:completed", *completed_ready)

    @staticmethod
    def _request_telemetry_persisted(request_id, redis_clients):
        """Return whether every terminal future copy is safe for cleanup."""
        for redis_client in redis_clients:
            for future_id in redis_client.smembers(f"request:{request_id}:futures"):
                future = redis_client.hgetall(f"future:{future_id}")
                if future and future.get("finished_at") and str(
                    future.get("telemetry_persisted")
                ) != "1":
                    return False
        return True

    # ------------------------------------------------------------------ #
    #  Runtime launching                                                  #
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
            return subprocess.run(cmd, capture_output=True, text=True)
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
                    "-i",
                    ssh_key_path,
                    ssh_target,
                    remote_cmd,
                ],
                capture_output=True,
                text=True,
            )

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
        with self._lifecycle_lock:
            self.running = False
            self._shutdown_event.set()
            run_thread = self._run_thread

        if run_thread is not None and run_thread is not threading.current_thread():
            run_thread.join(timeout=5)
            if run_thread.is_alive():
                logger.warning("Controller health loop did not stop within 5 seconds.")

        if not self.telemetry_poller.stop(timeout=5):
            logger.warning(
                "Telemetry poller did not stop within 5 seconds; continuing shutdown."
            )

        cleanup_thread = self._cleanup_thread
        if cleanup_thread is not None and cleanup_thread is not threading.current_thread():
            cleanup_thread.join(timeout=5)
            if cleanup_thread.is_alive():
                logger.warning("Controller cleanup worker did not stop within 5 seconds.")

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
