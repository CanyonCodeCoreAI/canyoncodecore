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
from flask import Flask
from flask import jsonify
from flask import request
from werkzeug.serving import make_server

from ventis.config_loader import load_config
from ventis.controller.agent_spec_loader import write_agent_specs
from ventis.controller.runtime_manager import RuntimeManager
from ventis.request_priority import DEFAULT_AGENT_PRIORITY
from ventis.request_priority import normalize_priority
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
    initial routing table to Redis, then enters a polling loop that periodically
    checks controller health and refreshes the table.

    Designed to be subclassed — override the _on_* hooks to extend behavior.
    """

    QUEUE_DEPTH_KEY_PREFIX = "queue_depth"
    ACTIVE_WORK_KEY_SUFFIX = "active_work"
    LIFECYCLE_SIGNAL_SUFFIX = "lifecycle"

    STATUS_INITIALIZING = "Initializing"
    STATUS_HEALTHY = "Healthy"
    STATUS_IDLING = "Idling"
    STATUS_SHUTTING_DOWN = "Shutting down"
    STATUS_DELETE_READY = "Delete Ready"

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
        self._synced_projects = set()  # (host, remote_dir) synced this session
        self._autoscale_policies = {}
        self._lifecycle_statuses = {}
        self._idle_since = {}
        self._draining_endpoints = set()
        self._admin_server = None
        self._admin_thread = None
        self.runtime_manager = RuntimeManager(self)

        # Clean up any stale containers from previous runs
        self._cleanup_stale_containers()

        # Launch Redis on each unique local node, then write central specs and
        # publish routing/policy snapshots to host Redis instances.
        self._launch_redis_containers()
        write_agent_specs(self.config_path, self.redis)
        self._write_resource_specs()
        self._load_and_write_policies()
        self._reconcile_agent_policies()
        self._publish_routing_state()
        self._start_admin_server()
        logger.info("Global controller initialized with %d controller(s).", len(self.controllers))

        # Start background cleanup thread
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    # ------------------------------------------------------------------ #
    #  Stale container cleanup                                             #
    # ------------------------------------------------------------------ #

    def _cleanup_stale_containers(self):
        """Remove only Redis containers from previous runs.

        Agent containers are now reused by RuntimeManager, so startup
        cleanup must not delete them preemptively.
        """
        logger.info("Checking for stale Redis containers from previous runs...")

        host_containers = {}

        for ctrl in self.controllers:
            user = ctrl.get("user")
            for host in self.runtime_manager.list_runtime_nodes([ctrl]):
                if host not in host_containers:
                    host_containers[host] = (user, set())
                host_containers[host][1].add(f"ventis-redis-{host.replace('.', '-')}")

        # Try to remove each one on its respective host
        for host, (user, container_names) in host_containers.items():
            for container_name in container_names:
                try:
                    self._run_cmd(
                        ["docker", "rm", "-f", container_name], host, user
                    )
                except Exception:
                    pass  # Container didn't exist, that's fine

        logger.info("Stale Redis container cleanup complete.")

    # ------------------------------------------------------------------ #
    #  Config                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_config(config_path):
        """Load the YAML config file."""
        return load_config(config_path)

    def reload_config(self):
        """Reload the config file and refresh routing metadata."""
        logger.info("Reloading config from %s", self.config_path)
        self.config = self._load_config(self.config_path)
        self.controllers = self.config.get("agents", [])
        self.poll_interval = self.config.get("poll_interval", 5)
        write_agent_specs(self.config_path, self.redis)
        self._load_and_write_policies()
        self._reconcile_agent_policies()
        self._publish_routing_state()

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

    def _load_policy_config(self):
        config_dir = os.path.dirname(os.path.abspath(self.config_path))
        policy_path = os.path.join(config_dir, "policy.yaml")

        if not os.path.isfile(policy_path):
            logger.info("No policy file found at %s, skipping policy setup.", policy_path)
            return {}

        with open(policy_path, "r") as f:
            return yaml.safe_load(f) or {}

    def _load_policy_rules(self, policy_config=None):
        """Load access policy rules from config/policy.yaml."""
        policy_config = policy_config if policy_config is not None else self._load_policy_config()

        rules = policy_config.get("rules", [])

        # Sort rules by specificity: most match keys first
        # This way the local controller can iterate and use the first matching rule.
        rules.sort(key=lambda r: len(r.get("match", {})), reverse=True)
        return rules

    def _load_autoscale_policies(self, policy_config=None):
        """Normalize autoscale policy entries from policy.yaml.

        Services without queue_length_scale_up_threshold and max_replicas are
        skipped. min_replicas defaults to the agent's configured replica count.
        """
        policy_config = policy_config if policy_config is not None else self._load_policy_config()
        autoscale = policy_config.get("autoscale", {})
        base_replicas = {
            item["name"]: int(item.get("replicas", 1))
            for item in self.config.get("agents", [])
            if isinstance(item, dict) and item.get("name")
        }
        normalized = {}
        for service, cfg in autoscale.items():
            if not isinstance(cfg, dict):
                continue
            threshold = cfg.get("queue_length_scale_up_threshold")
            max_replicas = cfg.get("max_replicas")
            if threshold is None or max_replicas is None:
                continue
            normalized[service] = {
                "queue_length_scale_up_threshold": int(threshold),
                "max_replicas": int(max_replicas),
                "min_replicas": int(cfg.get("min_replicas", base_replicas.get(service, 1))),
                "idle_seconds_before_scale_down": int(cfg.get("idle_seconds_before_scale_down", 60)),
            }
        return normalized

    def _load_and_write_policies(self):
        """Load policy rules and publish them to every host Redis."""
        policy_config = self._load_policy_config()
        rules = self._load_policy_rules(policy_config)
        self._autoscale_policies = self._load_autoscale_policies(policy_config)
        target_count = self.runtime_manager.publish_policy_rules(rules)

        logger.info("Policy rules written to %d Redis instance(s): %d rule(s)", target_count, len(rules))

    # Routing reads are direct Redis calls; RuntimeManager owns publication.

    def _agent_spec(self, agent_name):
        for ctrl in self.controllers:
            if ctrl.get("name") == agent_name:
                return ctrl
        return None

    def _agent_policy_key(self, agent_name):
        return f"agent:{agent_name}:"

    def _desired_agent_policy(self, agent_name):
        desired = {}
        central_priority = self.redis.hget(self._agent_policy_key(agent_name), "priority")
        if central_priority is not None:
            desired["priority"] = str(
                normalize_priority(central_priority, DEFAULT_AGENT_PRIORITY)
            )
        else:
            spec = self._agent_spec(agent_name)
            desired["priority"] = str(
                normalize_priority(
                    (spec or {}).get("priority"),
                    DEFAULT_AGENT_PRIORITY,
                )
            )
        return desired

    def _hosts_for_agent(self, agent_name):
        hosts = []
        seen = set()
        for instance in self.runtime_manager.list_instances(agent_name):
            host = instance.get("host")
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
        return hosts

    def _agent_policy_targets(self, agent_name):
        targets = []
        for host in self._hosts_for_agent(agent_name):
            redis_client = self.node_redis.get(host)
            if redis_client is not None:
                targets.append((host, redis_client))
        return targets

    def _sync_agent_policy_hash(self, redis_client, agent_name, desired_fields):
        key = self._agent_policy_key(agent_name)
        changed = {}
        for field, value in desired_fields.items():
            if redis_client.hget(key, field) != value:
                changed[field] = value
        if changed:
            redis_client.hset_multiple(key, changed)
        return changed

    def _propagate_agent_policy(self, agent_name):
        desired = self._desired_agent_policy(agent_name)
        updated_hosts = []
        for host, redis_client in self._agent_policy_targets(agent_name):
            if self._sync_agent_policy_hash(redis_client, agent_name, desired):
                updated_hosts.append(host)
        return updated_hosts

    def _reconcile_agent_policies(self):
        updates = {}
        for ctrl in self.controllers:
            name = ctrl.get("name")
            if not name:
                continue
            updated_hosts = self._propagate_agent_policy(name)
            if updated_hosts:
                updates[name] = updated_hosts
        return updates

    def update_agent_policy(self, agent_name, payload):
        spec = self._agent_spec(agent_name)
        if spec is None:
            raise KeyError(agent_name)
        if not isinstance(payload, dict):
            raise ValueError("policy update payload must be a JSON object")
        if "priority" not in payload:
            raise ValueError("priority is required")

        priority = normalize_priority(payload.get("priority"), None)
        if priority is None:
            raise ValueError("priority must be an integer")

        key = self._agent_policy_key(agent_name)
        self.redis.hset(key, "priority", str(priority))
        spec["priority"] = priority
        updated_hosts = self._propagate_agent_policy(agent_name)
        return {"agent": agent_name, "priority": priority, "updated_hosts": updated_hosts}

    def create_admin_app(self):
        app = Flask("ventis-global-controller-admin")

        @app.route("/policy/agents/<agent_name>", methods=["POST"])
        def update_policy(agent_name):
            payload = request.get_json(silent=True) or {}
            try:
                result = self.update_agent_policy(agent_name, payload)
            except KeyError:
                return jsonify({"error": f"unknown agent '{agent_name}'"}), 404
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify(result)

        return app

    def _start_admin_server(self):
        if self._admin_server is not None:
            return
        port = int(os.environ.get("VENTIS_ADMIN_PORT", self.config.get("admin_port", 8090)))
        host = self.config.get("admin_host", os.environ.get("VENTIS_ADMIN_HOST", "0.0.0.0"))
        app = self.create_admin_app()
        self._admin_server = make_server(host, port, app)
        self._admin_thread = threading.Thread(
            target=self._admin_server.serve_forever,
            daemon=True,
        )
        self._admin_thread.start()
        logger.info("Global controller admin API listening on http://%s:%s", host, port)

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
        nodes = self.runtime_manager.list_runtime_nodes()

        for host, node_cfg in nodes.items():
            redis_port = node_cfg["redis_port"]
            user = node_cfg["user"]
            self.ensure_host_redis(host, user, redis_port)

        logger.info("Redis launched on %d node(s).", len(self.redis_containers))

    def ensure_host_redis(self, host, user=None, redis_port=6379, ssh_host=None):
        """Launch/register the Redis container used by controllers on one host."""
        if host in self.node_redis:
            return self.node_redis[host]

        ssh_host = ssh_host or host
        prep_result = self._ensure_remote_docker(ssh_host, user)
        if prep_result.returncode != 0:
            logger.critical(
                "Failed to prepare Docker on %s: %s",
                ssh_host, prep_result.stderr.strip(),
            )
            sys.exit(1)

        container_name = f"ventis-redis-{host.replace('.', '-')}"
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{redis_port}:6379",
            "redis:alpine",
        ]

        try:
            result = self._run_cmd(cmd, ssh_host, user)
            if result.returncode != 0:
                logger.critical(
                    "Failed to launch Redis on %s: %s",
                    ssh_host, result.stderr.strip(),
                )
                sys.exit(1)
        except FileNotFoundError:
            logger.critical("Docker is not installed or not in PATH. Cannot launch Redis.")
            sys.exit(1)
        except Exception as e:
            logger.critical("Failed to launch Redis on %s: %s", ssh_host, e)
            sys.exit(1)

        self.redis_containers[host] = container_name
        connect_host = "localhost" if _is_local_host(host) else host
        node_redis = RedisClient(host=connect_host, port=redis_port)
        self._wait_for_redis(node_redis, connect_host, redis_port)
        self.node_redis[host] = node_redis
        self._reconcile_agent_policies()
        publish_policy_rules = getattr(self.runtime_manager, "publish_policy_rules", None)
        if publish_policy_rules:
            publish_policy_rules(self._load_policy_rules())
        logger.info("Launched Redis container %s on %s:%d", container_name, host, redis_port)
        return self.node_redis[host]

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
        nodes = self.runtime_manager.list_runtime_nodes()
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
        pending = [
            (instance["agent_name"], instance["host"], instance["host_port"])
            for instance in self.runtime_manager.list_instances()
        ]
        self._wait_for_pending_healthy(pending, timeout=timeout, interval=interval)
        self._publish_routing_state()

    def _wait_for_pending_healthy(self, pending, timeout=30, interval=2, require_healthy=False):
        """Poll node Redis until listed replicas report healthy.

        When require_healthy is False, logs a warning on timeout and returns.
        When True, raises TimeoutError (used during autoscale scale-up rollback).
        """
        pending = list(pending)
        deadline = time.time() + timeout

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
            if require_healthy:
                raise TimeoutError(f"Controllers not ready after {timeout}s: {pending}")

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
        for instance in self.runtime_manager.list_instances():
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
        self._reconcile_instance_lifecycle()
        self._scale_from_queue_depth()
        self._reconcile_agent_policies()

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

    def _queue_depth_key(self, name, host, port):
        return f"{self.QUEUE_DEPTH_KEY_PREFIX}:{name}:{self._agent_host_key(host)}:{port}"

    def _active_work_key(self, host, port):
        return f"controller:{self._agent_host_key(host)}:{port}:{self.ACTIVE_WORK_KEY_SUFFIX}"

    def _lifecycle_signal_key(self, host, port):
        return f"controller:{self._agent_host_key(host)}:{port}:{self.LIFECYCLE_SIGNAL_SUFFIX}"

    def _routing_endpoint(self, instance):
        return self.runtime_manager._routing_endpoint_for(instance)

    def _routing_status_payload(self):
        """Per-endpoint lifecycle status for routing_table:status publication."""
        payload = {}
        for instance in self.runtime_manager.list_instances():
            service = instance["agent_name"]
            endpoint = self._routing_endpoint(instance)
            payload.setdefault(service, {})[endpoint] = self._lifecycle_statuses.get(
                endpoint,
                self.STATUS_INITIALIZING,
            )
        return payload

    def _routable_endpoints_payload(self, extra_excluded_endpoints=None):
        """Endpoints that should receive new requests.

        Excludes draining endpoints and replicas in Shutting down or Delete Ready.
        Status may still list excluded endpoints so clients see the transition.
        """
        excluded = set(self._draining_endpoints)
        if extra_excluded_endpoints:
            excluded.update(extra_excluded_endpoints)
        payload = {}
        for instance in self.runtime_manager.list_instances():
            endpoint = self._routing_endpoint(instance)
            if endpoint in excluded:
                continue
            status = self._lifecycle_statuses.get(endpoint)
            if status in {self.STATUS_SHUTTING_DOWN, self.STATUS_DELETE_READY}:
                continue
            payload.setdefault(instance["agent_name"], set()).add(endpoint)
        return payload

    def _publish_routing_state(self, extra_excluded_endpoints=None):
        """Publish routing endpoints and lifecycle status to host Redis."""
        publish_routing_snapshot = getattr(self.runtime_manager, "publish_routing_snapshot", None)
        if publish_routing_snapshot:
            publish_routing_snapshot(
                self.controllers,
                lifecycle_statuses=self._routing_status_payload(),
                routable_endpoints=self._routable_endpoints_payload(extra_excluded_endpoints),
            )

    def _read_active_work(self, instance):
        raw = self._get_node_redis_for(instance["host"]).get(
            self._active_work_key(instance["host"], instance["host_port"])
        )
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def _read_lifecycle_signal(self, instance):
        return self._get_node_redis_for(instance["host"]).get(
            self._lifecycle_signal_key(instance["host"], instance["host_port"])
        )

    def _is_instance_healthy(self, instance):
        return self._last_status.get((instance["host"], instance["host_port"])) == "healthy"

    def _has_affine_inflight_requests(self, service, endpoint):
        for affinity_key in self.redis.scan_keys("affinity:*"):
            if affinity_key.count(":") != 1:
                continue
            request_endpoint = self.redis.hget(affinity_key, service)
            if request_endpoint != endpoint:
                continue
            request_id = affinity_key.split(":", 1)[1]
            request_status = self.redis.get(f"request:{request_id}:status")
            if request_status not in {"done", "error"}:
                return True
        return False

    def _mark_instance_status(self, instance, status):
        self._lifecycle_statuses[self._routing_endpoint(instance)] = status

    def _clear_instance_state(self, instance):
        endpoint = self._routing_endpoint(instance)
        self._lifecycle_statuses.pop(endpoint, None)
        self._idle_since.pop(endpoint, None)
        self._draining_endpoints.discard(endpoint)

    def _reconcile_instance_lifecycle(self):
        """Drive per-replica lifecycle and scale-down.

        Each poll: mark instances Healthy/Idling from queue depth and active work,
        pick one idling replica above min_replicas to drain (remove from routing,
        mark Shutting down), then remove instances whose local controller reports
        Delete Ready after draining.
        """
        now = time.time()
        delete_ready_instances = []

        for controller_spec in self.controllers:
            service = controller_spec["name"]
            policy = self._autoscale_policies.get(service)
            idle_window = int((policy or {}).get("idle_seconds_before_scale_down", 60))
            min_replicas = int((policy or {}).get("min_replicas", int(controller_spec.get("replicas", 1))))
            instances = sorted(
                self.runtime_manager.list_instances(service),
                key=lambda item: int(item["replica_index"]),
            )
            current_replicas = len(instances)

            for instance in instances:
                endpoint = self._routing_endpoint(instance)
                if not self._is_instance_healthy(instance):
                    self._idle_since.pop(endpoint, None)
                    if endpoint not in self._draining_endpoints:
                        self._lifecycle_statuses[endpoint] = self.STATUS_INITIALIZING
                    continue

                if endpoint in self._draining_endpoints:
                    signal = self._read_lifecycle_signal(instance)
                    if signal == self.STATUS_DELETE_READY:
                        self._lifecycle_statuses[endpoint] = self.STATUS_DELETE_READY
                        delete_ready_instances.append((controller_spec, instance))
                    else:
                        self._lifecycle_statuses[endpoint] = self.STATUS_SHUTTING_DOWN
                    continue

                active_work = self._read_active_work(instance)
                queue_depth = self._queue_depth_for_instance(instance)
                if queue_depth != 0 or active_work != 0:
                    self._idle_since.pop(endpoint, None)
                    self._lifecycle_statuses[endpoint] = self.STATUS_HEALTHY
                    continue
                if controller_spec.get("stateful") and self._has_affine_inflight_requests(service, endpoint):
                    self._idle_since.pop(endpoint, None)
                    self._lifecycle_statuses[endpoint] = self.STATUS_HEALTHY
                    continue
                idle_since = self._idle_since.setdefault(endpoint, now)
                if (now - idle_since) >= idle_window:
                    self._lifecycle_statuses[endpoint] = self.STATUS_IDLING
                else:
                    self._lifecycle_statuses[endpoint] = self.STATUS_HEALTHY

            if policy and current_replicas > min_replicas:
                candidates = [
                    instance
                    for instance in instances
                    if self._lifecycle_statuses.get(self._routing_endpoint(instance)) == self.STATUS_IDLING
                ]
                if candidates:
                    candidate = max(candidates, key=lambda item: int(item["replica_index"]))
                    endpoint = self._routing_endpoint(candidate)
                    self._draining_endpoints.add(endpoint)
                    self._publish_routing_state(extra_excluded_endpoints={endpoint})
                    self._lifecycle_statuses[endpoint] = self.STATUS_SHUTTING_DOWN

        self._publish_routing_state()

        for controller_spec, instance in delete_ready_instances:
            self._publish_routing_state()
            self.runtime_manager.remove_instance(
                self.runtime_manager._instance_id_from_record(instance),
                publish=False,
            )
            controller_spec["replicas"] = max(
                int((self._autoscale_policies.get(controller_spec["name"]) or {}).get("min_replicas", 1)),
                int(controller_spec.get("replicas", 1)) - 1,
            )
            self._clear_instance_state(instance)
            self._publish_routing_state()

    def _queue_depth_for_instance(self, instance):
        key = self._queue_depth_key(
            instance["agent_name"],
            instance["host"],
            instance["host_port"],
        )
        raw = self._get_node_redis_for(instance["host"]).get(key)
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def _queue_depth_by_service(self):
        queue_depths = {}
        for instance in self.runtime_manager.list_instances():
            depth = self._queue_depth_for_instance(instance)
            queue_depths[instance["agent_name"]] = queue_depths.get(instance["agent_name"], 0) + depth
        return queue_depths

    def _scale_from_queue_depth(self):
        # Scale services up when aggregate queue depth exceeds configured thresholds.
        """Check each service's aggregate queue depth and scale up if needed."""
        queue_depths = self._queue_depth_by_service()
        for controller_spec in self.controllers:
            self._scale_service(controller_spec, queue_depths)

    def _scale_service(self, controller_spec, queue_depths):
        # Add one replica when this service is over threshold and below max replicas.
        """Scale up one replica when aggregate queue depth exceeds policy threshold.

        Creates the new runtime with publish=False, publishes Initializing status,
        waits for health, then publishes Healthy. On failure, removes only the new
        replica and restores the previous replica count.
        """
        name = controller_spec["name"]
        policy = self._autoscale_policies.get(name)
        if not policy:
            return

        threshold = policy["queue_length_scale_up_threshold"]
        max_replicas = policy["max_replicas"]
        observed_depth = queue_depths.get(name, 0)
        current_replicas = len(self.runtime_manager.list_instances(name))

        if observed_depth <= threshold or current_replicas >= max_replicas:
            return

        target_replicas = min(max_replicas, max(current_replicas, int(controller_spec.get("replicas", 1))) + 1)
        if target_replicas <= current_replicas:
            return

        logger.info(
            "Scaling %s from %d to %d replicas (queue_depth=%d threshold=%d)",
            name,
            current_replicas,
            target_replicas,
            observed_depth,
            threshold,
        )

        previous_replicas = int(controller_spec.get("replicas", current_replicas or 1))
        controller_spec["replicas"] = target_replicas
        replica_index = target_replicas - 1
        rollback_instance_id = f"{controller_spec.get('provider', 'local')}:{name}:{replica_index}"
        instance = None
        try:
            instance = self.runtime_manager.ensure_replica(
                controller_spec,
                replica_index,
                publish=False,
            )
            self._mark_instance_status(instance, self.STATUS_INITIALIZING)
            self._publish_routing_state()
            self._wait_for_pending_healthy(
                [(instance["agent_name"], instance["host"], instance["host_port"])],
                timeout=30,
                interval=2,
                require_healthy=True,
            )
            self._mark_instance_status(instance, self.STATUS_HEALTHY)
            self._publish_routing_state()
        except Exception:
            self.runtime_manager.remove_instance(
                rollback_instance_id,
                publish=False,
            )
            controller_spec["replicas"] = previous_replicas
            if instance is not None:
                self._clear_instance_state(instance)
            raise

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
            for instance in self.runtime_manager.list_instances():
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
        ec2_cfg = getattr(self, "config", {}).get("ec2", {})
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
        ]
        ssh_key_path = ec2_cfg.get("ssh_private_key_path")
        if ssh_key_path:
            cmd.extend(["-i", ssh_key_path])
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

        self._ship_image_ssh(image, host, user)
        self._shipped_images.add((image, host))

    def _sync_project_to_host(self, host, user, remote_dir):
        """
        Mirror the current project directory to a fixed remote path.

        ponytail: this is a full-tree tar sync for MVP simplicity; replace with
        rsync or artifact packaging later if transfer size matters.
        """
        if _is_local_host(host):
            return

        sync_key = (host, remote_dir)
        if sync_key in self._synced_projects:
            logger.debug("Project already synced to %s:%s this session, skipping.", host, remote_dir)
            return

        ssh_target = self._ssh_target(host, user)
        remote_cmd = (
            f"sudo rm -rf {shlex.quote(remote_dir)} && "
            f"sudo mkdir -p {shlex.quote(remote_dir)} && "
            f"sudo tar -xzf - -C {shlex.quote(remote_dir)}"
        )
        tar_cmd = [
            "tar",
            "--exclude=.git",
            "--exclude=.omx",
            "--exclude=.pytest_cache",
            "--exclude=.venv",
            "--exclude=__pycache__",
            "--exclude=docker_container",
            "-czf",
            "-",
            ".",
        ]

        logger.info("Syncing project to %s:%s...", host, remote_dir)
        tar_proc = subprocess.Popen(
            tar_cmd,
            cwd=os.getcwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        load_proc = subprocess.Popen(
            [*self._ssh_base_cmd(), ssh_target, remote_cmd],
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tar_proc.stdout.close()
        _, stderr = load_proc.communicate()
        tar_stderr = tar_proc.stderr.read().decode().strip()
        tar_proc.stderr.close()
        tar_proc.wait()

        if tar_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create project archive for {host}: {tar_stderr or 'tar failed'}"
            )
        if load_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to sync project to {host}:{remote_dir}: {stderr.decode().strip()}"
            )

        self._synced_projects.add(sync_key)
        logger.info("Project synced to %s:%s.", host, remote_dir)

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

    def launch_agents(self):
        """Create or reuse agent containers through RuntimeManager."""
        try:
            self.containers = {}
            instances = self.runtime_manager.ensure_instances(self.controllers, publish=False)
            total = len(instances)
            logger.info(
                "Ensured %d Docker container(s) across %d service(s).",
                total,
                len(self.containers),
            )
        except FileNotFoundError:
            logger.critical("Docker is not installed or not in PATH. Cannot launch agents.")
            self._stop_redis_containers()
            sys.exit(1)
        except Exception:
            logger.exception("Failed to ensure agent runtimes")
            self._stop_docker_agents()
            self._stop_redis_containers()
            sys.exit(1)

    def _stop_docker_agents(self):
        """Stop and remove all managed runtimes."""
        for instance in list(self.runtime_manager.list_instances()):
            try:
                self.runtime_manager.remove_instance(
                    self.runtime_manager._instance_id_from_record(instance)
                )
                logger.info("Removed runtime %s", instance["runtime_id"])
            except Exception as e:
                logger.warning("Failed to remove runtime %s: %s", instance["runtime_id"], e)

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
        if self._admin_server is not None:
            self._admin_server.shutdown()
            self._admin_server.server_close()
            self._admin_server = None
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

    controller.launch_agents()
    controller._wait_for_healthy()
    controller.run()
