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

import yaml

from ventis.utils.redis_client import RedisClient

# Add generated grpc_stubs from the local project to the path
sys.path.insert(0, os.path.abspath("grpc_stubs"))
import local_controler_pb2
import local_controler_pb2_grpc
import grpc

print(f"DEBUG: Loading gRPC stubs from: {local_controler_pb2_grpc.__file__}")
print(f"DEBUG: LocalControllerStub attributes: {[a for a in dir(local_controler_pb2_grpc.LocalControllerStub) if not a.startswith('_')]}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        self.processes = {}  # name -> [Popen, ...]
        self.containers = {}  # name -> [container_name, ...]
        self.redis_containers = {}  # host -> container_name
        self.node_redis = {}  # host -> RedisClient
        self._last_status = {}  # (host, port) -> last known status
        self._lc_stubs = {}    # endpoint -> gRPC stub
        self._shipped_images = set()  # (image, host) already shipped this session

        # Registry config: if set, use push/pull; otherwise fall back to SSH pipe
        registry_cfg = self.config.get("registry", {})
        self.registry_url = registry_cfg.get("url")  # e.g. "myregistry.example.com:5000"
        self.registry_user = registry_cfg.get("user")

        # Clean up any stale containers from previous runs
        self._cleanup_stale_containers()

        # Launch Redis on each unique node, then write routing table and policies
        self._launch_redis_containers()
        self._build_routing_table()
        self._write_resource_specs()
        self._load_and_write_policies()
        self._write_output_policies()
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

                # Redis container for this host
                host_containers[host][1].add(f"ventis-redis-{host.replace('.', '-')}")
                # Agent container
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

    def reload_config(self):
        """Reload the config file and rebuild the routing table."""
        logger.info("Reloading config from %s", self.config_path)
        self.config = self._load_config(self.config_path)
        self.controllers = self.config.get("agents", [])
        self.poll_interval = self.config.get("poll_interval", 5)
        self._build_routing_table()

    # ------------------------------------------------------------------ #
    #  Routing table                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_replica_placements(ctrl):
        """Normalize the ``replicas`` field into a list of (host, port) tuples.

        Supports two formats in the YAML config:

        1. **Integer shorthand** — ``replicas: 3``
           Uses the agent's ``host`` and sequential ports starting at ``port``.

        2. **Explicit list** — each entry specifies its own ``host`` and ``port``:
           ::

               replicas:
                 - host: node1
                   port: 8051
                 - host: node2
                   port: 8052
        """
        replicas = ctrl.get("replicas", 1)
        default_host = ctrl.get("host", "localhost")
        base_port = ctrl.get("port", 50051)

        if isinstance(replicas, int):
            return [(default_host, base_port + i) for i in range(replicas)]
        elif isinstance(replicas, list):
            return [
                (r.get("host", default_host), r.get("port", base_port))
                for r in replicas
            ]
        else:
            return [(default_host, base_port)]

    def _build_routing_table(self):
        """Write the routing table to Redis on every node.

        For each agent, stores a JSON list of all replica endpoints under
        ``routing_table:endpoints``.  Agents marked ``stateful: true`` are
        recorded in ``routing_table:stateful`` so local controllers can
        enforce session affinity.
        """
        endpoints_table = {}   # name → JSON list of endpoints
        stateful_table = {}    # name → "true" (only for stateful agents)

        for ctrl in self.controllers:
            name = ctrl["name"]
            stateful = ctrl.get("stateful", False)
            placements = self._get_replica_placements(ctrl)

            endpoints = []
            for host, port in placements:
                # Use host.docker.internal for localhost so Docker containers
                # can reach each other through the host's port mappings.
                rt_host = "host.docker.internal" if host in ("localhost", "127.0.0.1") else host
                endpoints.append(f"{rt_host}:{port}")

            endpoints_table[name] = json.dumps(endpoints)
            if stateful:
                stateful_table[name] = "true"

        # Write to every node's Redis so each local controller can look up
        # the full routing table from its own Redis instance.
        targets = list(self.node_redis.values()) if self.node_redis else [self.redis]
        for redis_client in targets:
            if endpoints_table:
                redis_client.hset_multiple(self.ROUTING_ENDPOINTS_KEY, endpoints_table)
            if stateful_table:
                redis_client.hset_multiple(self.ROUTING_STATEFUL_KEY, stateful_table)

            existing = redis_client.smembers(self.SERVICES_SET_KEY)
            for stale in existing - set(endpoints_table.keys()):
                redis_client.srem(self.SERVICES_SET_KEY, stale)
            for name in endpoints_table.keys():
                redis_client.sadd(self.SERVICES_SET_KEY, name)

        logger.info("Routing table written to %d Redis instance(s): %s",
                     len(targets), endpoints_table)
        self._on_routing_table_updated(endpoints_table)

    def _write_resource_specs(self):
        """Write the per-agent resource specs to Redis."""
        for ctrl in self.controllers:
            name = ctrl["name"]
            resources = ctrl.get("resources", {})
            placements = self._get_replica_placements(ctrl)

            self.redis.hset_multiple(f"agent:{name}:resources", {
                "cpu": str(resources.get("cpu", 1)),
                "memory": str(resources.get("memory", 512)),
                "replicas": str(len(placements)),
            })

    def _write_output_policies(self):
        """Write each agent's configured output policies to Redis on every node.

        Output policies are declared per-agent in the controller config under
        ``output_policies`` as a list of ``module:function`` references. They are
        stored under ``agent:<name>:output_policies`` as a JSON list so the local
        controller running that agent can resolve and run them after execution.
        Agents without any output policies get an empty list (safe no-op).
        """
        targets = list(self.node_redis.values()) if self.node_redis else [self.redis]
        total = 0
        for ctrl in self.controllers:
            name = ctrl["name"]
            policies = ctrl.get("output_policies", []) or []
            policies_json = json.dumps(policies)
            for redis_client in targets:
                redis_client.set(f"agent:{name}:output_policies", policies_json)
            if policies:
                total += 1
                logger.info("Output policies for %s: %s", name, policies)

        logger.info("Output policies written to %d Redis instance(s) for %d agent(s).",
                    len(targets), total)

    def _load_and_write_policies(self):
        """Load policy rules from config/policy.yaml and write to all Redis instances."""
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

        rules_json = json.dumps(rules)

        # Write to every node's Redis
        targets = list(self.node_redis.values()) if self.node_redis else [self.redis]
        for redis_client in targets:
            redis_client.set(self.POLICY_RULES_KEY, rules_json)

        logger.info("Policy rules written to %d Redis instance(s): %d rule(s)", len(targets), len(rules))

    def get_routing_table(self):
        """Read the current routing table from Redis."""
        return self.redis.hgetall(self.ROUTING_TABLE_KEY)

    def get_endpoint(self, service_name):
        """Look up the endpoint for a given service."""
        return self.redis.hget(self.ROUTING_TABLE_KEY, service_name)

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

    def _stop_redis_containers(self):
        """Stop and remove all launched Redis containers."""
        for host, container_name in self.redis_containers.items():
            # Find the SSH user for this host by checking all replica placements
            user = None
            for ctrl in self.controllers:
                for rhost, _rport in self._get_replica_placements(ctrl):
                    if rhost == host:
                        user = ctrl.get("user")
                        break
                if user is not None:
                    break
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
        return "host.docker.internal" if host in ("localhost", "127.0.0.1") else host

    def _wait_for_healthy(self, timeout=30, interval=2):
        """
        Block until all controllers report healthy in Redis, or until timeout.

        Args:
            timeout:  Maximum seconds to wait.
            interval: Seconds between checks.
        """
        deadline = time.time() + timeout
        # Build a list of every individual replica to check
        pending = []
        for c in self.controllers:
            name = c["name"]
            for host, port in self._get_replica_placements(c):
                pending.append((name, host, port))

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
        for ctrl in self.controllers:
            name = ctrl["name"]
            for host, port in self._get_replica_placements(ctrl):
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
            for ctrl in self.controllers:
                for host, port in self._get_replica_placements(ctrl):
                    endpoint = f"{host}:{port}"
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
    #  Agent launching                                                    #
    # ------------------------------------------------------------------ #
    def launch_agents(self):
        """
        Launch all agents defined in the config.

        For each controller entry, spawn `replicas` number of subprocesses
        using the configured entrypoint script. Each replica gets assigned
        a port starting from the controller's base port.
        """
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )

        for ctrl in self.controllers:
            name = ctrl["name"]
            placements = self._get_replica_placements(ctrl)
            entrypoint = ctrl.get("entrypoint")

            if not entrypoint:
                logger.warning("No entrypoint for %s, skipping launch.", name)
                continue

            entrypoint_path = os.path.join(project_root, entrypoint)
            if not os.path.isfile(entrypoint_path):
                logger.error("Entrypoint not found: %s", entrypoint_path)
                continue

            self.processes[name] = []
            for host, port in placements:
                proc = self._launch_single_agent(name, entrypoint_path, port, ctrl, host)
                if proc:
                    self.processes[name].append(proc)

        total = sum(len(procs) for procs in self.processes.values())
        logger.info("Launched %d agent process(es) across %d service(s).",
                    total, len(self.processes))

    def _launch_single_agent(self, name, entrypoint_path, port, ctrl, host=None):
        """
        Launch a single agent subprocess.

        Args:
            name: Service/agent name.
            entrypoint_path: Absolute path to the agent script.
            port: Port number for this instance.
            ctrl: Full controller config dict.

        Returns:
            The Popen object, or None on failure.
        """
        resources = ctrl.get("resources", {})
        env = os.environ.copy()
        env["VENTIS_AGENT_NAME"] = name
        env["VENTIS_AGENT_PORT"] = str(port)
        env["VENTIS_AGENT_CPU"] = str(resources.get("cpu", 1))
        env["VENTIS_AGENT_MEMORY"] = str(resources.get("memory", 512))

        try:
            proc = subprocess.Popen(
                [sys.executable, entrypoint_path, "--port", str(port)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("Launched %s (pid=%d) on port %d", name, proc.pid, port)

            # Record status in Redis
            host = host or ctrl.get("host", "localhost")
            self.redis.set(f"controller:{host}:{port}:status", "healthy")
            self.redis.set(f"controller:{host}:{port}:pid", str(proc.pid))

            return proc
        except Exception as e:
            logger.error("Failed to launch %s on port %d: %s", name, port, e)
            return None

    def _stop_agents(self):
        """Terminate all launched agent subprocesses."""
        for name, procs in self.processes.items():
            for proc in procs:
                if proc.poll() is None:  # still running
                    logger.info("Terminating %s (pid=%d)", name, proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning("Killing %s (pid=%d)", name, proc.pid)
                        proc.kill()
        self.processes.clear()
        logger.info("All agent processes stopped.")

    # ------------------------------------------------------------------ #
    #  Docker launching                                                   #
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
        is_local = host in ("localhost", "127.0.0.1")
        if is_local:
            return subprocess.run(cmd, capture_output=True, text=True)
        else:
            ssh_target = f"{user}@{host}" if user else host
            remote_cmd = " ".join(cmd)
            return subprocess.run(
                ["ssh", ssh_target, remote_cmd],
                capture_output=True, text=True,
            )

    def _ensure_image_on_host(self, image, host, user):
        """
        Ensure `image` is available on `host` before running a container.

        Strategy:
        - If a registry is configured: tag + push to registry, then pull on
          the remote host via SSH.
        - Fallback (no registry): stream the image over SSH using
          ``docker save | ssh ... docker load``.

        Images are only shipped once per (image, host) pair per session.
        Does nothing for localhost.
        """
        if host in ("localhost", "127.0.0.1"):
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

        # Retag to the expected short name on the remote so container launch works
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
        ssh_target = f"{user}@{host}" if user else host
        logger.info(
            "Shipping image %s to %s via SSH pipe (no registry configured)...",
            image, host,
        )
        save_proc = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
        )
        load_proc = subprocess.Popen(
            ["ssh", ssh_target, "docker load"],
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
        """
        Launch all agents as Docker containers.

        For each agent in the config, runs `docker run` either locally or
        via SSH on the specified host. Spawns `replicas` containers per agent,
        each on an incrementing port from the base port.

        Assumes Docker images are pre-built (via `make docker`).
        Image name convention: ventis-<agentname_lowercase>
        """
        for ctrl in self.controllers:
            name = ctrl["name"]
            default_host = ctrl.get("host", "localhost")
            user = ctrl.get("user")
            resources = ctrl.get("resources", {})
            ctrl_type = ctrl.get("type", "agent")
            placements = self._get_replica_placements(ctrl)

            image = f"ventis-{name.lower()}"
            self.containers[name] = []

            for i, (host, port) in enumerate(placements):
                container_name = f"ventis-{name.lower()}-{i}"

                # Containers can't reach host's Redis via "localhost";
                # use host.docker.internal to route to the Docker host.
                redis_host_for_container = "host.docker.internal" if host in ("localhost", "127.0.0.1") else host

                cmd = [
                    "docker", "run", "-d", "-it",
                    "--add-host=host.docker.internal:host-gateway",
                    "--name", container_name,
                    "-p", f"{port}:50051",
                    "-e", f"VENTIS_AGENT_PORT={port}",
                    "-e", f"VENTIS_AGENT_HOST={'host.docker.internal' if host in ('localhost', '127.0.0.1') else host}",
                    "-e", f"VENTIS_REDIS_HOST={redis_host_for_container}",
                    "-e", f"VENTIS_REDIS_PORT={ctrl.get('redis_port', 6379)}",
                ]

                # Workflow containers also expose the REST API port
                if ctrl_type == "workflow":
                    api_port = ctrl.get("api_port", 8080)
                    cmd.extend(["-p", f"{api_port}:8080"])

                # Apply resource limits
                cpu = resources.get("cpu")
                memory = resources.get("memory")
                gpu = resources.get("gpu")
                if cpu:
                    cmd.extend(["--cpus", str(cpu)])
                if memory:
                    cmd.extend(["--memory", f"{memory}m"])
                if gpu:
                    # Provide the specific count or identifier requested (e.g., '1', '2', 'all')
                    cmd.extend(["--gpus", str(gpu)])

                cmd.append(image)

                try:
                    # Ensure the image exists on the target host before launching
                    replica_user = user  # TODO: per-replica user support
                    self._ensure_image_on_host(image, host, replica_user)
                    result = self._run_cmd(cmd, host, replica_user)
                    if result.returncode == 0:
                        container_id = result.stdout.strip()[:12]
                        self.containers[name].append(container_name)
                        logger.info(
                            "Launched container %s (%s) on %s:%d",
                            container_name, container_id, host, port,
                        )
                    else:
                        logger.critical(
                            "Failed to launch %s on %s:%d: %s",
                            container_name, host, port, result.stderr.strip(),
                        )
                        # Remove the failed container left in "Created" state
                        self._run_cmd(["docker", "rm", "-f", container_name], host, user)
                        self._stop_docker_agents()
                        self._stop_redis_containers()
                        sys.exit(1)
                except FileNotFoundError:
                    logger.critical("Docker is not installed or not in PATH. Cannot launch agents.")
                    self._stop_redis_containers()
                    sys.exit(1)
                except Exception as e:
                    logger.critical(
                        "Failed to launch %s on %s:%d: %s",
                        container_name, host, port, e,
                    )
                    self._run_cmd(["docker", "rm", "-f", container_name], host, user)
                    self._stop_docker_agents()
                    self._stop_redis_containers()
                    sys.exit(1)

        total = sum(len(c) for c in self.containers.values())
        logger.info("Launched %d Docker container(s) across %d service(s).",
                    total, len(self.containers))

    def _stop_docker_agents(self):
        """Stop and remove all launched Docker containers."""
        for ctrl in self.controllers:
            name = ctrl["name"]
            user = ctrl.get("user")
            placements = self._get_replica_placements(ctrl)

            containers_for_agent = self.containers.get(name, [])
            for i, container_name in enumerate(containers_for_agent):
                # Match each container to its placement host
                if i < len(placements):
                    host = placements[i][0]
                else:
                    host = ctrl.get("host", "localhost")

                try:
                    self._run_cmd(["docker", "stop", container_name], host, user)
                    self._run_cmd(["docker", "rm", container_name], host, user)
                    logger.info("Stopped and removed %s on %s", container_name, host)
                except Exception as e:
                    logger.warning("Failed to stop %s on %s: %s", container_name, host, e)

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
