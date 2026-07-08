# Global Controller
# Daemon process that maintains a routing table in Redis for multiple local controllers.
# Periodically polls Redis to check controller health and updates the routing table.

import atexit
import logging
import signal
import shlex
import subprocess
import threading
import time
import json
import sys
import os

import yaml
from ventis.controller.instance_manager import InstanceManager
from ventis.utils.redis_client import RedisClient

# Add generated grpc_stubs from the local project to the path
sys.path.insert(0, os.path.abspath("grpc_stubs"))
import local_controler_pb2
import local_controler_pb2_grpc
import grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def write_agent_specs(config_path, redis_client):
    """Read global_controller.yaml and write agent specs to Redis."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    for agent in config.get("agents", []):
        name = agent["name"]
        spec_class = "WorkflowSpec" if agent.get("type") == "workflow" else "AgentSpec"
        redis_client.hset_multiple(
            f"agent:{name}:",
            {
                "class": spec_class,
                "resources": json.dumps(agent.get("resources", {})),
                "replicas": json.dumps(agent.get("replicas", 1)),
                "stateful": json.dumps(agent.get("stateful", False)),
                "redis_port": str(agent.get("redis_port", 6379)),
                "provider": agent.get("provider", "local"),
            },
        )


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
        self._lc_stubs = {}    # endpoint -> gRPC stub
        self._shipped_images = set()  # (image, host) already shipped this session
        self.instance_manager = InstanceManager(self)
        registry_cfg = self.config.get("registry", {})
        self.registry_url = registry_cfg.get("url")
        self.registry_user = registry_cfg.get("user")

        # Clean up any stale containers from previous runs
        self._cleanup_stale_containers()

        # Launch Redis on each unique node, then write routing table and policies
        self._launch_redis_containers()
        write_agent_specs(self.config_path, self.redis)
        self._write_resource_specs()
        self._load_and_write_policies()
        self.instance_manager.publish_routing_snapshot(self.controllers)
        logger.info("Global controller initialized with %d controller(s).", len(self.controllers))

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
                    self._run_cmd(
                        ["docker", "rm", "-f", container_name], host, user
                    )
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
        self.instance_manager.publish_routing_snapshot(self.controllers)

    def _write_resource_specs(self):
        """Write the per-agent resource specs to Redis."""
        for ctrl in self.controllers:
            name = ctrl["name"]
            resources = ctrl.get("resources", {})
            self.redis.hset_multiple(f"agent:{name}:resources", {
                "cpu": str(resources.get("cpu", 1)),
                "memory": str(resources.get("memory", 512)),
                "replicas": str(int(ctrl.get("replicas", 1))),
            })

    def _load_policy_rules(self):
        """Load policy rules from config/policy.yaml."""
        config_dir = os.path.dirname(os.path.abspath(self.config_path))
        policy_path = os.path.join(config_dir, "policy.yaml")

        if not os.path.isfile(policy_path):
            logger.info("No policy file found at %s, skipping policy setup.", policy_path)
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

        logger.info("Policy rules written to %d Redis instance(s): %d rule(s)", len(targets), len(rules))

    # Routing reads are direct Redis calls now that InstanceManager owns publication:
    # - self.redis.hgetall(self.ROUTING_ENDPOINTS_KEY)
    # - self.redis.hget(self.ROUTING_ENDPOINTS_KEY, service_name)

    def get_node_redis(self, host):
        """Get the RedisClient for a specific node."""
        return self.node_redis.get(host)

    # ------------------------------------------------------------------ #
    #  Redis container management                                         #
    # ------------------------------------------------------------------ #

    def _launch_redis_containers(self):
        """
        Launch a Redis Docker container on each unique node.

        Discovers unique hosts from the agent config and starts one
        redis:alpine container per host. Creates a RedisClient instance
        for each node so the global controller can query any node's Redis.
        """
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

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "-p", f"{redis_port}:6379",
                "redis:alpine",
            ]

            try:
                result = self._run_cmd(cmd, host, user)
                if result.returncode == 0:
                    self.redis_containers[host] = container_name
                    logger.info(
                        "Launched Redis container %s on %s:%d",
                        container_name, host, redis_port,
                    )
                else:
                    logger.critical(
                        "Failed to launch Redis on %s: %s",
                        host, result.stderr.strip(),
                    )
                    sys.exit(1)
            except FileNotFoundError:
                logger.critical("Docker is not installed or not in PATH. Cannot launch Redis.")
                sys.exit(1)
            except Exception as e:
                logger.critical("Failed to launch Redis on %s: %s", host, e)
                sys.exit(1)

            # Create a RedisClient for this node
            # For localhost, connect directly; for remote, connect via host IP
            connect_host = "localhost" if host in ("localhost", "127.0.0.1") else host
            self.node_redis[host] = RedisClient(
                host=connect_host, port=redis_port,
            )

        # Update the primary redis client to the local node's Redis
        if "localhost" in self.node_redis:
            self.redis = self.node_redis["localhost"]

        logger.info("Redis launched on %d node(s).", len(self.redis_containers))

    def _wait_for_redis(self, redis_client, host, port, timeout=30, interval=1):
        """Wait until Redis accepts commands, surfacing network issues clearly."""
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                redis_client.set("__ventis_redis_healthcheck__", "ok")
                return
            except Exception as exc:
                last_error = exc
                time.sleep(interval)

        raise TimeoutError(
            f"Timed out connecting to Redis at {host}:{port}. "
            "For EC2 runtimes, ensure the instance security group allows inbound "
            f"TCP {port} from the global controller host."
        ) from last_error

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

        logger.info("Waiting for %d replica(s) to become healthy (timeout=%ds)...",
                    len(pending), timeout)

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
                    name, host, port, timeout,
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
                self._poll_controllers()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.stop()

    def _poll_controllers(self):
        """Check the health of each registered controller replica via its node's Redis."""
        for instance in self.instance_manager.list_instances():
            name = instance["agent_name"]
            host = instance["host"]
            port = instance["host_port"]
            node_redis = self._get_node_redis_for(host)
            agent_host = self._agent_host_key(host)
            status_key = f"controller:{agent_host}:{port}:status"

            status = node_redis.get(status_key) or "unknown"
            prev = self._last_status.get((host, port))

            if status != prev:
                if status == "healthy":
                    logger.info("Controller %s (%s:%s) is now healthy.", name, host, port)
                    self._on_controller_healthy(name, host, port)
                else:
                    logger.warning(
                        "Controller %s (%s:%s) status changed: %s -> %s",
                        name, host, port, prev or "(none)", status,
                    )
                    self._on_controller_unhealthy(name, host, port)
                self._last_status[(host, port)] = status
            else:
                # No change — healthy stays quiet, unhealthy stays quiet too
                if status == "healthy":
                    self._on_controller_healthy(name, host, port)
                else:
                    self._on_controller_unhealthy(name, host, port)

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
            self._lc_stubs[endpoint] = local_controler_pb2_grpc.LocalControllerStub(channel)
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
        """Broadcast Cleanup gRPC to all local controllers for each completed request."""
        completed = self.redis.smembers("request:completed")
        if not completed:
            return

        for request_id in completed:
            logger.info("Triggering cleanup for completed request %s", request_id)
            for instance in self.instance_manager.list_instances():
                endpoint = instance["endpoint"]
                try:
                    stub = self._get_lc_stub(endpoint)
                    payload = json.dumps({"request_id": request_id})
                    stub.Cleanup(local_controler_pb2.JsonResponse(resonse=payload))
                    logger.debug("Sent Cleanup for request %s to %s", request_id, endpoint)
                except Exception as e:
                    logger.warning("Failed to trigger cleanup on %s: %s", endpoint, e)

            # Remove from completed set after broadcast
            self.redis.srem("request:completed", request_id)

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
            ssh_target = self._ssh_target(host, user)
            remote_cmd = " ".join(cmd)
            if cmd and cmd[0] == "docker":
                remote_cmd = f"sudo {remote_cmd}"
            return subprocess.run(
                [*self._ssh_base_cmd(), ssh_target, remote_cmd],
                capture_output=True, text=True,
            )

    def _ssh_target(self, host, user=None):
        """Build the SSH target string for a remote host."""
        return f"{user}@{host}" if user else host

    def _ssh_base_cmd(self):
        """Return the shared SSH command prefix for remote EC2 operations."""
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
        ]
        return cmd

    def _run_remote_script(self, host, script, user=None):
        """Run a shell script on a remote host over SSH."""
        if _is_local_host(host):
            return subprocess.run(["bash", "-lc", script], capture_output=True, text=True)
        return subprocess.run(
            [*self._ssh_base_cmd(), self._ssh_target(host, user), "bash", "-lc", shlex.quote(script)],
            capture_output=True,
            text=True,
        )

    def _ensure_remote_docker(self, host, user=None):
        """Install and start Docker on a remote host if needed."""
        if _is_local_host(host):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        wait_result = self._wait_for_remote_ssh(host, user)
        if wait_result.returncode != 0:
            return wait_result
        script = """
set -e
if ! command -v docker >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y docker.io
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y docker
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y docker
  else
    echo unsupported-package-manager >&2
    exit 1
  fi
fi
sudo systemctl enable --now docker || sudo service docker start
""".strip()
        return self._run_remote_script(host, script, user)

    def _wait_for_remote_ssh(self, host, user=None, timeout=120, interval=2):
        """Wait until a remote host accepts SSH connections."""
        if _is_local_host(host):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        deadline = time.time() + timeout
        last_result = None
        while time.time() < deadline:
            result = subprocess.run(
                [*self._ssh_base_cmd(), self._ssh_target(host, user), "true"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result

            last_result = result
            stderr = (result.stderr or "").lower()
            if "permission denied" in stderr:
                return result

            time.sleep(interval)

        return last_result or subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"Timed out waiting for SSH on {host}",
        )

    def _ensure_image_on_host(self, image, host, user):
        """
        Ensure `image` is available on `host` before running a container.

        Images are only shipped once per (image, host) pair per session.
        Does nothing for localhost.
        """
        if _is_local_host(host):
            return  # already on the local Docker engine

        if (image, host) in self._shipped_images:
            logger.debug("Image %s already shipped to %s this session, skipping.", image, host)
            return

        if self.registry_url:
            self._ship_image_registry(image, host, user)
        else:
            self._ship_image_ssh(image, host, user)
        self._shipped_images.add((image, host))

    def _ship_image_registry(self, image, host, user):
        """
        Push image to the configured registry on the local host, then
        instruct the remote host to pull it.
        """
        remote_tag = f"{self.registry_url}/{image}:latest"
        logger.info("Tagging %s -> %s for registry push...", image, remote_tag)
        subprocess.run(["docker", "tag", image, remote_tag], check=True)

        logger.info("Pushing %s to registry %s...", image, self.registry_url)
        subprocess.run(["docker", "push", remote_tag], check=True)

        logger.info("Pulling %s on %s from registry...", image, host)
        result = self._run_cmd(["docker", "pull", remote_tag], host, user)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to pull {remote_tag} on {host}: {result.stderr.strip()}"
            )

        retag_result = self._run_cmd(
            ["docker", "tag", remote_tag, f"{image}:latest"],
            host, user,
        )
        if retag_result.returncode != 0:
            logger.warning("Failed to retag %s on %s: %s", remote_tag, host, retag_result.stderr.strip())

        logger.info("Image %s is ready on %s (via registry).", image, host)

    def _ship_image_ssh(self, image, host, user):
        """
        Stream image to remote host using `docker save | ssh docker load`.
        Used as a fallback when no registry is configured.
        """
        ssh_target = self._ssh_target(host, user)
        logger.info(
            "Shipping image %s to %s via SSH pipe (no registry configured)...",
            image, host,
        )
        save_proc = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
        )
        load_proc = subprocess.Popen(
            [*self._ssh_base_cmd(), ssh_target, "sudo docker load"],
            stdin=save_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        save_proc.stdout.close()
        _, stderr = load_proc.communicate()
        save_proc.wait()

        if load_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to ship image {image} to {host} via SSH: {stderr.decode().strip()}"
            )
        logger.info("Image %s shipped to %s successfully.", image, host)

    def launch_docker_agents(self):
        """Launch all configured runtimes through InstanceManager."""
        try:
            instances = self.instance_manager.ensure_instances(self.controllers)
        except FileNotFoundError:
            logger.critical("Docker is not installed or not in PATH. Cannot launch agents.")
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
            len({instance['agent_name'] for instance in instances}),
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
        "-c", "--config",
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
    atexit.register(controller.cleanup)

    controller.launch_docker_agents()
    controller._wait_for_healthy()
    controller.run()
