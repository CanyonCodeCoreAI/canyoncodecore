# Local Controller
# Starts the gRPC frontend server and polls the request queue for incoming requests.
# Routes requests to the correct agent — either locally or by forwarding to another controller.

import json
import logging
import os
import random
import sys
import time
import importlib.util
import threading
from queue import Empty
from concurrent.futures import ThreadPoolExecutor

import grpc

try:
    from ventis.controller.local_controller_frontend import start_server
    from ventis.controller.local_controller_frontend import build_queue_item
    from ventis.utils.redis_client import RedisClient
except ImportError:
    from local_controller_frontend import start_server
    from local_controller_frontend import build_queue_item
    from redis_client import RedisClient

# Add local generated grpc_stubs to path (Docker context copies them directly to /app)
sys.path.insert(0, ".")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath("grpc_stubs"))

try:
    import ventis.ventis_context as ventis_context
    from ventis.request_priority import (
        DEFAULT_AGENT_PRIORITY,
        DEFAULT_SESSION_PRIORITY,
        apply_effective_priorities,
        normalize_priority,
    )
except ImportError:
    import ventis_context
    from request_priority import (
        DEFAULT_AGENT_PRIORITY,
        DEFAULT_SESSION_PRIORITY,
        apply_effective_priorities,
        normalize_priority,
    )
import local_controler_pb2
import local_controler_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
ROUTING_STATUS_KEY = "routing_table:status"
ROUTING_STATEFUL_KEY = "routing_table:stateful"
POLICY_RULES_KEY = "policy:rules"
QUEUE_DEPTH_KEY_PREFIX = "queue_depth"
ACTIVE_WORK_KEY_SUFFIX = "active_work"
LIFECYCLE_SIGNAL_SUFFIX = "lifecycle"
STATUS_SHUTTING_DOWN = "Shutting down"
STATUS_DELETE_READY = "Delete Ready"


def _is_request_affinity_key(affinity_key):
    """Return True for request-scoped affinity:<request_id> keys only."""
    return affinity_key.count(":") == 1


class LocalController(object):
    """Manages the gRPC frontend and processes incoming requests from the queue."""

    def __init__(self, port=50051):
        self.port = port
        self.agent_host = os.environ.get("VENTIS_AGENT_HOST", "localhost")
        self.agent_name = os.environ.get("VENTIS_AGENT_NAME")
        self.agent_file = os.environ.get("VENTIS_AGENT_FILE")
        
        # Public port is how the routing table and other nodes know us;
        # internally the gRPC server binds to `port` (50051 inside Docker).
        self.public_port = os.environ.get("VENTIS_AGENT_PORT", str(port))
        
        self._my_endpoint = f"{self.agent_host}:{self.public_port}"

        self.server, self.servicer = start_server(port, my_endpoint=self._my_endpoint)
        self.request_queue = self.servicer.request_queue

        # Connect to Redis and report healthy status
        redis_host = os.environ.get("VENTIS_REDIS_HOST", "localhost")
        redis_port = int(os.environ.get("VENTIS_REDIS_PORT", 6379))
        self.redis = RedisClient(host=redis_host, port=redis_port)
        self._status_key = f"controller:{self.agent_host}:{self.public_port}:status"
        self._active_work_key_name = f"controller:{self.agent_host}:{self.public_port}:{ACTIVE_WORK_KEY_SUFFIX}"
        self._lifecycle_signal_key = f"controller:{self.agent_host}:{self.public_port}:{LIFECYCLE_SIGNAL_SUFFIX}"
        self.redis.set(self._status_key, "healthy")
        self.redis.set(self._active_work_key_name, "0")
        self.redis.delete(self._lifecycle_signal_key)
        self._publish_queue_depth()
        self.agent_priority = self._load_configured_priority()
        if hasattr(self.servicer, "set_agent_priority"):
            self.servicer.set_agent_priority(self.agent_priority)

        # Cache for gRPC stubs to remote controllers
        self._remote_channels = {}  # endpoint -> grpc.Channel
        self._remote_stubs = {}     # endpoint -> LocalControllerStub

        # Policy rules cache (loaded lazily from Redis)
        self._policy_rules = None

        # Thread pool for executing agent methods concurrently.
        # This prevents deadlocks when an agent method creates nested Futures
        # that need to be routed through the same controller's request queue.
        max_instances = int(os.environ.get("VENTIS_MAX_AGENT_INSTANCES", 8))
        self._executor = ThreadPoolExecutor(max_workers=max_instances)
        self._active_requests = 0
        self._active_requests_lock = threading.Lock()
        self._shutting_down = False

        logger.info("Local controller initialized at %s (max_agent_instances=%d), reported healthy to Redis.", self._my_endpoint, max_instances)
        
        # Load the agent class dynamically
        self.agent = self._load_agent()

    def _load_configured_priority(self):
        """Load this controller's configured priority once at startup."""
        env_priority = os.environ.get("VENTIS_AGENT_PRIORITY")
        if env_priority is not None:
            return normalize_priority(env_priority, DEFAULT_AGENT_PRIORITY)
        if not self.agent_name:
            return DEFAULT_AGENT_PRIORITY
        return normalize_priority(
            self.redis.hget(f"agent:{self.agent_name}:", "priority"),
            DEFAULT_AGENT_PRIORITY,
        )

    def _runtime_priority(self):
        if not self.agent_name:
            return DEFAULT_AGENT_PRIORITY
        return normalize_priority(
            self.redis.hget(f"agent:{self.agent_name}:", "priority"),
            self.agent_priority,
        )

    def _rebuild_pending_queue_for_priority(self):
        drained = []
        while True:
            try:
                drained.append(self.request_queue.get_nowait())
            except Empty:
                break
        if not drained:
            return 0

        rebuilt = 0
        for queued in drained:
            raw = queued[3] if isinstance(queued, tuple) else queued
            enqueue_order = queued[2] if isinstance(queued, tuple) and len(queued) > 2 else None
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                self.request_queue.put(queued)
                continue
            session_priority, _, _ = apply_effective_priorities(
                data=data,
                context=data.get("baggage", {}).get("context"),
                default_session=DEFAULT_SESSION_PRIORITY,
                default_agent=self.agent_priority,
            )
            self.request_queue.put(
                build_queue_item(
                    json.dumps(data),
                    session_priority,
                    self.agent_priority,
                    enqueue_order=enqueue_order,
                )
            )
            rebuilt += 1
        self._publish_queue_depth()
        return rebuilt

    def _refresh_runtime_policy(self):
        latest_priority = self._runtime_priority()
        if latest_priority == self.agent_priority:
            return False
        previous = self.agent_priority
        self.agent_priority = latest_priority
        if hasattr(self.servicer, "set_agent_priority"):
            self.servicer.set_agent_priority(latest_priority)
        rebuilt = self._rebuild_pending_queue_for_priority()
        logger.info(
            "Updated %s priority from %s to %s and rebuilt %d queued request(s).",
            self.agent_name,
            previous,
            latest_priority,
            rebuilt,
        )
        return True

    def _load_agent(self):
        """Dynamically load and instantiate the agent class."""
        if not self.agent_name or not self.agent_file:
            logger.warning("VENTIS_AGENT_NAME or VENTIS_AGENT_FILE not set. Running without an agent.")
            return None

        agent_module_name = self.agent_file.replace(".py", "")
        
        # We assume the agent file is in the same directory as the local controller (e.g. copied by Docker)
        # or in the current working directory.
        agent_path = os.path.abspath(str(self.agent_file))
        
        if not os.path.exists(agent_path):
            logger.error(f"Agent file not found at {agent_path}")
            return None

        try:
            spec = importlib.util.spec_from_file_location(agent_module_name, agent_path)
            if spec is None or getattr(spec, "loader", None) is None:
                logger.error(f"Cannot find spec or loader for module {agent_module_name} at {agent_path}")
                return None
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[agent_module_name] = module
            spec.loader.exec_module(module)
            
            agent_class = getattr(module, self.agent_name)
            agent_instance = agent_class()
            logger.info(f"Successfully loaded and instantiated agent: {self.agent_name}")
            return agent_instance
        except Exception as e:
            logger.error(f"Failed to load agent {self.agent_name} from {agent_path}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Policy evaluation                                                   #
    # ------------------------------------------------------------------ #

    def _load_policy_rules(self):
        """Load policy rules from Redis (cached after first load)."""
        if self._policy_rules is not None:
            return self._policy_rules

        rules_json = self.redis.get(POLICY_RULES_KEY)
        if rules_json:
            self._policy_rules = json.loads(rules_json)
        else:
            self._policy_rules = []
        return self._policy_rules

    def _queue_depth_key(self):
        return f"{QUEUE_DEPTH_KEY_PREFIX}:{self.agent_name}:{self.agent_host}:{self.public_port}"

    def _current_queue_depth(self):
        return self.request_queue.qsize() + getattr(self, "_active_requests", 0)

    def _publish_queue_depth(self):
        if not self.agent_name:
            return
        self.redis.set(self._queue_depth_key(), str(self._current_queue_depth()))

    def _publish_active_work(self):
        key = getattr(self, "_active_work_key_name", None)
        if not key:
            return
        self.redis.set(key, str(max(0, getattr(self, "_active_requests", 0))))

    def _increment_active_requests(self):
        lock = getattr(self, "_active_requests_lock", None)
        if lock is None:
            self._active_requests = getattr(self, "_active_requests", 0) + 1
            self._publish_active_work()
            return
        with lock:
            self._active_requests += 1
        self._publish_active_work()

    def _decrement_active_requests(self):
        lock = getattr(self, "_active_requests_lock", None)
        if lock is None:
            self._active_requests = max(0, getattr(self, "_active_requests", 0) - 1)
            self._publish_active_work()
            return
        with lock:
            self._active_requests = max(0, self._active_requests - 1)
        self._publish_active_work()

    def _is_shutting_down(self):
        """Return True when routing status marks this endpoint as Shutting down."""
        if not self.agent_name:
            return False
        raw = self.redis.hget(ROUTING_STATUS_KEY, self.agent_name)
        if not raw:
            return self._shutting_down
        try:
            statuses = json.loads(raw)
        except json.JSONDecodeError:
            return self._shutting_down
        if statuses.get(self._my_endpoint) == STATUS_SHUTTING_DOWN:
            self._shutting_down = True
        return self._shutting_down

    def _has_affine_inflight_requests(self):
        if not self.agent_name:
            return False
        for affinity_key in self.redis.scan_keys("affinity:*"):
            if not _is_request_affinity_key(affinity_key):
                continue
            if self.redis.hget(affinity_key, self.agent_name) != self._my_endpoint:
                continue
            request_id = affinity_key.split(":", 1)[1]
            status = self.redis.get(f"request:{request_id}:status")
            if status not in {"done", "error"}:
                return True
        return False

    def _is_delete_ready(self):
        """Return True when local queue and active work are drained."""
        if not self.request_queue.empty():
            return False
        if getattr(self, "_active_requests", 0) != 0:
            return False
        is_stateful = self.redis.hget(ROUTING_STATEFUL_KEY, self.agent_name) == "true"
        if is_stateful and self._has_affine_inflight_requests():
            return False
        return True

    def _update_lifecycle_signal(self):
        """Tell the global controller this replica has finished draining.

        Writes Delete Ready to Redis only when routing status is Shutting down
        and local queue, active work, and stateful affinity constraints are clear.
        """
        if self._is_shutting_down() and self._is_delete_ready():
            self.redis.set(self._lifecycle_signal_key, STATUS_DELETE_READY)
        else:
            self.redis.delete(self._lifecycle_signal_key)

    def _check_policy(self, service, context):
        """
        Check if the given service is accessible for the given request context.

        Iterates through rules (sorted most-specific first) and returns True
        if a matching rule grants access to the service.
        """
        rules = self._load_policy_rules()
        if not rules:
            # No policy rules -> allow everything
            return True

        for rule in rules:
            match = rule.get("match", {})
            access = rule.get("access", [])

            # Check if all match keys are satisfied by the request context
            if all(context.get(k) == v for k, v in match.items()):
                if access == "all":
                    return True
                return service in access

        # No rule matched at all
        logger.warning("No policy rule matched for context=%s, denying access to %s", context, service)
        return False

    # ------------------------------------------------------------------ #
    #  Endpoint resolution (affinity / load balancing)                      #
    # ------------------------------------------------------------------ #

    def _resolve_endpoint(self, service, request_id, agent_id=None):
        """Pick the correct endpoint for a service.

        - **Stateful agents**: check for an existing affinity binding in
          Redis (``affinity:<service>:<agent_id>``). If none exists,
          pick a random replica and persist the binding so later requests
          that reuse the same agent_id land on the same instance.
          When a request_id is available, also mirror the chosen endpoint
          into ``affinity:<request_id>`` so the existing per-request cleanup
          and forwarding baggage keep working.
          If no agent_id is available, fall back to the legacy request-scoped
          affinity behavior.
        - **Stateless agents**: pick a random replica from the endpoint
          list on every call.

        Returns:
            The chosen endpoint string, or ``None`` if the service is not
            in the routing table.
        """
        endpoints_json = self.redis.hget(ROUTING_ENDPOINTS_KEY, service)
        if not endpoints_json:
            return None

        endpoints = json.loads(endpoints_json)
        if not endpoints:
            return None

        # Check if this agent is stateful
        is_stateful = self.redis.hget(ROUTING_STATEFUL_KEY, service) == "true"

        if is_stateful and agent_id:
            affinity_key = f"affinity:{service}:{agent_id}"
            existing = self.redis.hget(affinity_key, "endpoint")
            if existing:
                if request_id:
                    self.redis.hset(f"affinity:{request_id}", service, existing)
                logger.debug("Affinity hit: %s -> %s (agent %s)", service, existing, agent_id)
                return existing
            chosen = random.choice(endpoints)
            self.redis.hset(affinity_key, "endpoint", chosen)
            if request_id:
                self.redis.hset(f"affinity:{request_id}", service, chosen)
            logger.info("Affinity set: %s -> %s (agent %s)", service, chosen, agent_id)
            return chosen
        if is_stateful and request_id:
            affinity_key = f"affinity:{request_id}"
            existing = self.redis.hget(affinity_key, service)
            if existing:
                logger.debug("Affinity hit: %s -> %s (request %s)", service, existing, request_id)
                return existing
            chosen = random.choice(endpoints)
            self.redis.hset(affinity_key, service, chosen)
            logger.info("Affinity set: %s -> %s (request %s)", service, chosen, request_id)
            return chosen
        # Stateless: pick randomly
        return random.choice(endpoints)

    # ------------------------------------------------------------------ #
    #  Request processing                                                  #
    # ------------------------------------------------------------------ #

    def run(self):
        """Poll the request queue and process incoming requests."""
        logger.info("Local controller started, polling request queue...")
        try:
            while True:
                self._refresh_runtime_policy()
                self._publish_queue_depth()
                self._publish_active_work()
                self._update_lifecycle_signal()
                if not self.request_queue.empty():
                    queued = self.request_queue.get()
                    raw = queued[3] if isinstance(queued, tuple) else queued
                    self._publish_queue_depth()
                    try:
                        data = json.loads(raw)
                        self._process_request(data)
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON in request: %s", raw)
                    except Exception as e:
                        logger.error("Error processing request: %s", e)
                else:
                    time.sleep(0.001)
        except KeyboardInterrupt:
            self.stop()

    def _process_request(self, data):
        """
        Route a request to the correct controller.

        Looks up the service in the routing table. If the endpoint matches
        this controller, execute locally. Otherwise, forward via gRPC.
        """
        service = data.get("service")
        function = data.get("function")
        args = data.get("args", {})
        future_id = data.get("future_id")
        origin = data.get("origin")  # endpoint of the LC that originated this request
        request_id = data.get("request_id")  # tracing ID from deploy module
        agent_id = data.get("agent_id")
        baggage = data.get("baggage", {})

        # 1. Unpack context from baggage (or fall back to local Redis)
        context = baggage.get("context")
        if context is None:
            context = {}
            if request_id:
                context_json = self.redis.get(f"request:{request_id}:context")
                if context_json:
                    context = json.loads(context_json)
        else:
            if request_id:
                # Cache received context locally for downstream stubs
                self.redis.set(f"request:{request_id}:context", json.dumps(context))
        session_priority, agent_priority, context = apply_effective_priorities(
            data=data,
            context=context,
            default_session=DEFAULT_SESSION_PRIORITY,
            default_agent=DEFAULT_AGENT_PRIORITY,
        )
        if request_id:
            self.redis.set(f"request:{request_id}:context", json.dumps(context))
        if agent_id is None:
            agent_id = context.get("agent_id")

        # 2. Unpack affinities from baggage into local Redis Hash
        affinities = baggage.get("affinities", {})
        if request_id and affinities:
            self.redis.hset_multiple(f"affinity:{request_id}", affinities)

        if not service or not function or not future_id:
            logger.error("Malformed request, missing required fields: %s", data)
            return

        # Check policy before routing
        if not self._check_policy(service, context):
            err_msg = f"Unauthorized: Policy denied access to service '{service}'"
            logger.warning(err_msg)
            self.redis.hset(f"future:{future_id}", "result", err_msg)
            if origin and origin != self._my_endpoint:
                self._send_result_callback(origin, future_id, err_msg)
            return

        # Resolve which endpoint to route to
        endpoint = self._resolve_endpoint(service, request_id, agent_id=agent_id)
        if not endpoint:
            logger.error("No endpoint found for service '%s' in routing table.", service)
            return

        if endpoint == self._my_endpoint:
            self._increment_active_requests()
            self._publish_queue_depth()
            self._executor.submit(
                self._execute_locally,
                service,
                function,
                args,
                future_id,
                origin,
                request_id,
                agent_id,
            )
        else:
            # Register the target as a consumer for any Future args
            # so results get pushed to its Redis via WriteResult.
            for key, value in args.items():
                if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value):
                    future_key = f"future:{value}"
                    if self.redis.hget(future_key, "id") is not None:
                        self.redis.sadd(f"{future_key}:consumers", endpoint)
                        logger.info("Registered %s as consumer of future %s (arg '%s')", endpoint, value, key)

                        # If the result is already available, push it immediately.
                        # This handles the race where _notify_consumers already ran.
                        existing_result = self.redis.hget(future_key, "result")
                        if existing_result is not None and existing_result != "":
                            logger.info("Future %s already resolved, pushing value %s to %s", value, existing_result, endpoint)
                            self._send_result_callback(endpoint, value, existing_result)

            # Build comprehensive outward baggage so the receiver gets all context and routing descisions
            outbound_baggage = {"context": context} if context else {}
            if request_id:
                all_affs = self.redis.hgetall(f"affinity:{request_id}")
                if all_affs:
                    outbound_baggage["affinities"] = all_affs

            if outbound_baggage:
                data["baggage"] = outbound_baggage

            # Note: We now rely on baggage["affinities"] heavily instead of `target_endpoint`.
            # We explicitly place the destined endpoint into the affinities bag.
            if request_id and "affinities" not in data.get("baggage", {}):
                data.setdefault("baggage", {})["affinities"] = {service: endpoint}
            elif request_id:
                data["baggage"]["affinities"][service] = endpoint

            logger.info("Forwarding %s.%s (future=%s) to %s", service, function, future_id, endpoint)
            self._forward_request(endpoint, data)

    def _resolve_future_args(self, args, poll_interval=0.01, timeout=300):
        """
        Check each arg value. If it is a 32-character hex string, assume it's
        a Future ID. Poll Redis until the result is available and replace
        the arg with the resolved value.
        """
        resolved = {}
        for key, value in args.items():
            # Check if this arg value is a UUID hex string identifying a future
            if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value):
                future_key = f"future:{value}"
                logger.info("Arg '%s' looks like a Future UUID (%s), waiting for result...", key, value)
                start = time.time()
                while True:
                    # print("Waiting for result for future next iteration %s", value)
                    result = self.redis.hget(future_key, "result")
                    if result is not None and result != "":
                        logger.info("Future %s resolved for arg '%s'", value, key)
                        resolved[key] = result
                        break
                    if time.time() - start > timeout:
                        raise TimeoutError(
                            f"Timed out waiting for future {value} (arg '{key}') "
                            f"after {timeout}s"
                        )
                    time.sleep(poll_interval)
                print("Resolved arg '%s' to %s", key, resolved[key])
            else:
                resolved[key] = value
        return resolved

    def _execute_locally(self, service, function, args, future_id, origin=None, request_id=None, agent_id=None):
        """Execute a request on the local agent and write the result to Redis."""
        # Propagate the request_id context into this worker thread
        ventis_context.set_request_id(request_id)
        ventis_context.set_agent_id(agent_id)
        context = {}
        if request_id:
            context_json = self.redis.get(f"request:{request_id}:context")
            if context_json:
                context = json.loads(context_json)
        session_priority, _, _ = apply_effective_priorities(
            context=context,
            default_session=DEFAULT_SESSION_PRIORITY,
            default_agent=self.agent_priority,
        )
        ventis_context.set_session_priority(session_priority)
        ventis_context.set_agent_priority(self.agent_priority)

        if self.agent is None:
            logger.error("No agent loaded, cannot execute %s.%s", service, function)
            return

        method = getattr(self.agent, function, None)
        if method is None:
            logger.error("Agent %s has no method '%s'", self.agent_name, function)
            return

        try:
            # Resolve any Future IDs in the args before executing
            args = self._resolve_future_args(args)

            logger.info("Executing %s.%s (future=%s) locally", service, function, future_id)
            result = method(**args)

            # Serialize the result
            if isinstance(result, (dict, list)):
                serialized = json.dumps(result)
            else:
                serialized = str(result)

            # Write result to local Redis
            self.redis.hset(f"future:{future_id}", "result", serialized)

            # If the request came from another node, send result back to origin
            if origin and origin != self._my_endpoint:
                self._send_result_callback(origin, future_id, serialized)

            logger.info("Completed %s.%s (future=%s) -> %s", service, function, future_id, serialized)
        except Exception as e:
            logger.error("Failed to execute %s.%s: %s", service, function, e)
            
            # Treat script-level crash as a string result to avoid hanging
            self.redis.hset(f"future:{future_id}", "result", f"Execution failed: {e}")
            if origin and origin != self._my_endpoint:
                self._send_result_callback(origin, future_id, f"Execution failed: {e}")
        finally:
            ventis_context.set_request_id(None)
            ventis_context.set_agent_id(None)
            ventis_context.set_session_priority(None)
            ventis_context.set_agent_priority(None)
            self._decrement_active_requests()
            self._publish_queue_depth()

    # ------------------------------------------------------------------ #
    #  Request forwarding                                                  #
    # ------------------------------------------------------------------ #

    def _get_remote_stub(self, endpoint):
        """Get or create a cached gRPC stub for a remote controller."""
        if endpoint not in self._remote_stubs:
            self._remote_channels[endpoint] = grpc.insecure_channel(endpoint)
            self._remote_stubs[endpoint] = local_controler_pb2_grpc.LocalControllerStub(
                self._remote_channels[endpoint]
            )
            logger.info("Created gRPC connection to remote controller at %s", endpoint)
        return self._remote_stubs[endpoint]

    def _forward_request(self, endpoint, data):
        """Forward a request to a remote controller via gRPC."""
        # Tag the request with our endpoint so the remote LC can call back
        data["origin"] = self._my_endpoint
        apply_effective_priorities(
            data=data,
            context=data.get("baggage", {}).get("context"),
            default_session=DEFAULT_SESSION_PRIORITY,
            default_agent=DEFAULT_AGENT_PRIORITY,
        )
        stub = self._get_remote_stub(endpoint)
        request = local_controler_pb2.JsonResponse(resonse=json.dumps(data))
        try:
            stub.Execute(request)
            logger.debug("Forwarded request to %s", endpoint)
        except Exception as e:
            logger.error("Failed to forward request to %s: %s", endpoint, e)

    def _send_result_callback(self, origin, future_id, result):
        """Send a result back to the originating controller via WriteResult RPC."""
        if not result:
            logger.warning("Agent '%s' is sending an empty/None result for future %s to origin %s, result: %s", self.agent_name, future_id, origin, result)

        stub = self._get_remote_stub(origin)
        payload = json.dumps({"future_id": future_id, "result": result})
        logger.info("Payload: Future %s,Sent %s ", future_id, payload)
        request = local_controler_pb2.JsonResponse(resonse=payload)
        try:
            stub.WriteResult(request)
            logger.info("Sent result callback to %s for future %s, result %s", origin, future_id, result)

        except Exception as e:
            logger.error("Failed to send result callback to %s: %s", origin, e)

    # ------------------------------------------------------------------ #
    #  Shutdown                                                            #
    # ------------------------------------------------------------------ #

    def stop(self):
        """Gracefully shut down the server."""
        logger.info("Shutting down local controller...")
        self._executor.shutdown(wait=True)
        self.redis.set(self._status_key, "stopped")
        if getattr(self, "_active_work_key_name", None):
            self.redis.set(self._active_work_key_name, "0")
        if getattr(self, "_lifecycle_signal_key", None):
            self.redis.delete(self._lifecycle_signal_key)
        if self.agent_name:
            self.redis.set(self._queue_depth_key(), "0")
        self.server.stop(0)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50051)
    args = parser.parse_args()

    controller = LocalController(port=args.port)
    controller.run()
