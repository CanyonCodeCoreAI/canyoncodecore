# Local Controller
# Starts the gRPC frontend server and polls the request queue for incoming requests.
# Routes requests to the correct agent — either locally or by forwarding to another controller.

import json
import logging
import os
import random
import sys
import threading
import time
import importlib.util
from concurrent.futures import ThreadPoolExecutor

import grpc
import psutil

try:
    from ventis.controller.local_controller_frontend import start_server
    from ventis.controller.utils.gpu_metrics import read_gpu_percent
    from ventis.controller.utils.redis_client import RedisClient
    from ventis.controller.utils.grpc_options import GRPC_CHANNEL_OPTIONS
except ImportError:
    from gpu_metrics import read_gpu_percent
    from local_controller_frontend import start_server
    from redis_client import RedisClient
    from grpc_options import GRPC_CHANNEL_OPTIONS

# Add local generated grpc_stubs to path (Docker context copies them directly to /app)
sys.path.insert(0, ".")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath("grpc_stubs"))

try:
    import ventis.controller.ventis_context as ventis_context
except ImportError:
    import ventis_context

# Auto-inject X-Ventis-Future-ID into all boto3 Bedrock calls so the LLM proxy
# can attribute token/cost telemetry to the executing future. Import for its
# global boto3 event-hook side effect; safe no-op if the proxy isn't present.
try:
    from ventis.llm_proxy import proxy as _llm_proxy_autoinject  # noqa: F401
except ImportError:
    try:
        from llm_proxy import proxy as _llm_proxy_autoinject  # noqa: F401
    except ImportError:
        pass  # No proxy available; agents call Bedrock directly.

import local_controler_pb2
import local_controler_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROUTING_ENDPOINTS_KEY = "routing_table:endpoints"
ROUTING_STATEFUL_KEY = "routing_table:stateful"
POLICY_RULES_KEY = "policy:rules"


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
        # Let the gRPC servicer fan a result out to this node's consumers as
        # soon as it arrives via WriteResult (see _fan_out_to_consumers).
        self.servicer.on_result = self._fan_out_to_consumers

        # Connect to Redis and report healthy status
        redis_host = os.environ.get("VENTIS_REDIS_HOST", "localhost")
        redis_port = int(os.environ.get("VENTIS_REDIS_PORT", 6379))
        self.redis = RedisClient(host=redis_host, port=redis_port)
        self._status_key = f"controller:{self.agent_host}:{self.public_port}:status"
        self.redis.set(self._status_key, "healthy")

        # Set once by InstanceManager when this replica was provisioned; read back
        # here so completed requests can be stamped with which replica ran them.
        self.agent_id = self.redis.get(
            f"controller:{self.agent_host}:{self.public_port}:agent_id"
        )

        # Periodically publish instance metrics, on the same cadence
        # GlobalController polls with (via VENTIS_POLL_INTERVAL).
        self._metrics_key = f"controller:{self.agent_host}:{self.public_port}:metrics"
        self._metrics_interval = float(os.environ.get("VENTIS_POLL_INTERVAL", 5))
        psutil.cpu_percent(interval=None)  # prime so the first real reading isn't 0.0
        self._metrics_stop_event = threading.Event()
        self._metrics_thread = threading.Thread(target=self._metrics_loop, daemon=True)
        self._metrics_thread.start()

        # Cache for gRPC stubs to remote controllers
        self._remote_channels = {}  # endpoint -> grpc.Channel
        self._remote_stubs = {}  # endpoint -> LocalControllerStub

        # Policy rules cache (loaded lazily from Redis)
        self._policy_rules = None

        # Thread pool for executing agent methods concurrently.
        # This prevents deadlocks when an agent method creates nested Futures
        # that need to be routed through the same controller's request queue.
        max_instances = int(os.environ.get("VENTIS_MAX_AGENT_INSTANCES", 8))
        self._executor = ThreadPoolExecutor(max_workers=max_instances)

        # Start the LLM proxy alongside the agent in this container. Bedrock
        # calls are routed to it via AWS_ENDPOINT_URL_BEDROCK_RUNTIME (injected
        # by the runtime), and it writes token/cost telemetry to Redis.
        self._proxy_process = self._start_llm_proxy(redis_host, redis_port)

        logger.info(
            "Local controller initialized at %s (max_agent_instances=%d), reported healthy to Redis.",
            self._my_endpoint,
            max_instances,
        )

        # Load the agent class dynamically
        self.agent = self._load_agent()

    def _start_llm_proxy(self, redis_host, redis_port):
        """Start the LLM proxy as a subprocess in this container (127.0.0.1:8081).

        Best-effort: a failure here must never stop the controller from coming up.
        """
        import subprocess

        try:
            proxy_env = os.environ.copy()
            proxy_env.update({
                "PROXY_HOST": "127.0.0.1",
                "PROXY_PORT": "8081",
                "VENTIS_REDIS_HOST": redis_host,
                "VENTIS_REDIS_PORT": str(redis_port),
            })
            proxy_process = subprocess.Popen(
                [sys.executable, "-m", "ventis.llm_proxy"],
                env=proxy_env,
            )
            logger.info(
                "Started LLM proxy on 127.0.0.1:8081 (PID: %d)", proxy_process.pid
            )
            return proxy_process
        except Exception as e:
            logger.warning("Failed to start LLM proxy: %s", e)
            return None

    def _collect_metrics(self):
        """Snapshot current instance health/resource metrics.

        requests_served is deliberately absent here -- it's incremented directly on
        the metrics hash (see _execute_locally) and drained by GlobalController after
        it reads it, not self-reset on this heartbeat's own timer. Self-resetting it
        here would silently drop counts for any interval GlobalController's poll
        loop falls behind on.
        """
        return {
            "status": "healthy",
            "cpu_percent": str(psutil.cpu_percent(interval=None)),
            "gpu_percent": str(read_gpu_percent()),
            "disk_percent": str(psutil.disk_usage("/").percent),
            "memory_percent": str(psutil.virtual_memory().percent),
            "uptime_seconds": str(max(time.time() - psutil.boot_time(), 0.0)),
            "queue_length": str(self._executor._work_queue.qsize()),
            "updated_at": str(time.time()),
        }

    def _metrics_loop(self):
        """Background thread: periodically publish instance metrics and refresh status."""
        while not self._metrics_stop_event.is_set():
            try:
                metrics = self._collect_metrics()
                self.redis.hset_multiple(self._metrics_key, metrics)
                self.redis.set(self._status_key, "healthy")
            except Exception as e:
                logger.warning("Metrics loop encountered an error: %s", e)
            self._metrics_stop_event.wait(self._metrics_interval)

    def _load_agent(self):
        """Dynamically load and instantiate the agent class."""
        if not self.agent_name or not self.agent_file:
            logger.warning(
                "VENTIS_AGENT_NAME or VENTIS_AGENT_FILE not set. Running without an agent."
            )
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
                logger.error(
                    f"Cannot find spec or loader for module {agent_module_name} at {agent_path}"
                )
                return None
            loader = spec.loader
            assert loader is not None

            module = importlib.util.module_from_spec(spec)
            sys.modules[agent_module_name] = module
            loader.exec_module(module)

            agent_class = getattr(module, self.agent_name)
            agent_instance = agent_class()
            logger.info(
                f"Successfully loaded and instantiated agent: {self.agent_name}"
            )
            return agent_instance
        except Exception as e:
            logger.error(
                f"Failed to load agent {self.agent_name} from {agent_path}: {e}"
            )
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
        logger.warning(
            "No policy rule matched for context=%s, denying access to %s",
            context,
            service,
        )
        return False

    # ------------------------------------------------------------------ #
    #  Endpoint resolution (affinity / load balancing)                      #
    # ------------------------------------------------------------------ #

    def _resolve_endpoint(self, service, request_id):
        """Pick the correct endpoint for a service.

        - **Stateful agents**: check for an existing affinity binding in
          Redis (``affinity:<request_id>:<service>``).  If none exists,
          pick a random replica and persist the binding so all subsequent
          calls within the same request land on the same instance.
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

        if is_stateful and request_id:
            affinity_key = f"affinity:{request_id}"
            existing = self.redis.hget(affinity_key, service)
            if existing:
                logger.debug(
                    "Affinity hit: %s -> %s (request %s)", service, existing, request_id
                )
                return existing
            # No existing binding — pick randomly and persist to Hash
            chosen = random.choice(endpoints)
            self.redis.hset(affinity_key, service, chosen)
            logger.info(
                "Affinity set: %s -> %s (request %s)", service, chosen, request_id
            )
            return chosen
        else:
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
                if not self.request_queue.empty():
                    raw = self.request_queue.get()
                    data = None
                    try:
                        data = json.loads(raw)
                        self._process_request(data)
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON in request: %s", raw)
                    except Exception as e:
                        logger.error("Error processing request: %s", e)
                        self._mark_future_failed(
                                data.get("future_id"), e, data.get("origin")
                        )
                else:
                    time.sleep(0.001)
        except KeyboardInterrupt:
            self.stop()

    def _mark_future_failed(self, future_id, error, origin=None):
        """Persist a terminal failure locally and, when needed, notify the origin."""
        if not future_id:
            return

        error_message = str(error) or "Unknown error"
        self.redis.hset_multiple(
            f"future:{future_id}",
            {"error": error_message, "failed": 1},
        )

        # Unblock any consumers waiting on this future so a failed dependency
        # surfaces as an error instead of a 300s timeout.
        self._fan_out_to_consumers(future_id, failed=1, error_message=error_message)

        if origin and origin != self._my_endpoint:
            self._send_result_callback(
                origin,
                future_id,
                failed=1,
                error_message=error_message,
            )

    def _process_request(self, data):
        """
        Route a request to the correct controller.

        Looks up the service in the routing table. If the endpoint matches
        this controller, execute locally. Otherwise, forward via gRPC.
        """
        service = data.get("service")
        function = data.get("function")
        args = data.get("args", {})
        parent = data.get("parent")
        future_id = data.get("future_id")
        origin = data.get("origin")  # endpoint of the LC that originated this request
        request_id = data.get("request_id")  # tracing ID from deploy module
        created_at = data.get("created_at")  # origin's true submission time
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

        # 2. Unpack affinities from baggage into local Redis Hash
        affinities = baggage.get("affinities", {})
        if request_id and affinities:
            self.redis.hset_multiple(f"affinity:{request_id}", affinities)

        if not service or not function or not future_id:
            logger.error("Malformed request, missing required fields: %s", data)
            self._mark_future_failed(
                future_id,
                "Malformed request: missing service, function, or future_id",
                origin,
            )
            return

        # Check policy before routing
        if not self._check_policy(service, context):
            err_msg = f"Unauthorized: Policy denied access to service '{service}'"
            logger.warning(err_msg)
            self._mark_future_failed(future_id, err_msg, origin)
            return

        # Resolve which endpoint to route to.
        # If the request was already routed to a specific node (route_to set by
        # the forwarding controller), honor that decision instead of resolving
        # again. Re-resolving at every hop lets stateless requests ping-pong
        # between replicas — the routing decision is made once, at the entry
        # node, and pinned for the rest of the request's journey.
        route_to = data.get("route_to")
        if route_to:
            endpoint = route_to
        else:
            endpoint = self._resolve_endpoint(service, request_id)
            if not endpoint:
                logger.error(
                    "No endpoint found for service '%s' in routing table.", service
                )
                self._mark_future_failed(
                    future_id,
                    f"No endpoint found for service '{service}'",
                    origin,
                )
                return

        if endpoint == self._my_endpoint:
            submitted_at = time.time()
            self._executor.submit(
                self._execute_locally,
                service,
                function,
                args,
                future_id,
                origin,
                request_id,
                submitted_at,
                parent,
                created_at,
            )
        else:
            # Register the target as a consumer for any Future args
            # so results get pushed to its Redis via WriteResult.
            for key, value in args.items():
                if (
                    isinstance(value, str)
                    and len(value) == 32
                    and all(c in "0123456789abcdefABCDEF" for c in value)
                ):
                    future_key = f"future:{value}"
                    if self.redis.hget(future_key, "id") is not None:
                        self.redis.sadd(f"{future_key}:consumers", endpoint)
                        logger.info(
                            "Registered %s as consumer of future %s (arg '%s')",
                            endpoint,
                            value,
                            key,
                        )

                        # If the result is already available, push it immediately.
                        # This handles the race where the producer resolved the
                        # future before this consumer registered (and thus before
                        # _fan_out_to_consumers could see it).
                        existing_result = self.redis.hget(future_key, "result")
                        if existing_result is not None and existing_result != "":
                            logger.info(
                                "Future %s already resolved, pushing value %s to %s",
                                value,
                                existing_result,
                                endpoint,
                            )
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

            logger.info(
                "Forwarding %s.%s (future=%s) to %s",
                service,
                function,
                future_id,
                endpoint,
            )
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
            if (
                isinstance(value, str)
                and len(value) == 32
                and all(c in "0123456789abcdefABCDEF" for c in value)
            ):
                future_key = f"future:{value}"
                logger.info(
                    "Arg '%s' looks like a Future UUID (%s), waiting for result...",
                    key,
                    value,
                )
                start = time.time()
                while True:
                    error = self.redis.hget(future_key, "error")
                    if error:
                        raise RuntimeError(error)
                    failed = self.redis.hget(future_key, "failed")
                    if str(failed) == "1":
                        raise RuntimeError(
                            self.redis.hget(future_key, "error")
                            or "Unknown error"
                        )
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

    def _execute_locally(
        self,
        service,
        function,
        args,
        future_id,
        origin=None,
        request_id=None,
        submitted_at=None,
        parent=None,
        created_at=None,
    ):
        """Execute a request on the local agent and write the result to Redis."""
        wall_start = time.time()
        thread_cpu_start = time.thread_time()

        # Write a complete, self-contained execution record for this step entirely
        # to this node's own Redis.
        initial_fields = {
            "id": future_id,
            "request_id": request_id or "",
            "result": "",
            "parent": parent or "",
            "service": service,
            "method": function,
            "args": json.dumps(args),
            "failed": 0,
            "error": "",
        }
      
        if created_at is not None: 
            initial_fields["created_at"] = created_at
        self.redis.hset_multiple(f"future:{future_id}", initial_fields)
        if request_id:
            self.redis.sadd(f"request:{request_id}:futures", future_id)
            ventis_context.set_request_id(request_id)
        ventis_context.set_current_future_id(future_id)
        ventis_context.set_current_metrics_key(self._metrics_key)
        if self.agent is None:
            logger.error("No agent loaded, cannot execute %s.%s", service, function)
            self._mark_future_failed(future_id, "No agent loaded", origin)
            return

        method = getattr(self.agent, function, None)
        if method is None:
            logger.error("Agent %s has no method '%s'", self.agent_name, function)
            self._mark_future_failed(
                future_id, f"Agent {self.agent_name} has no method '{function}'", origin
            )
            return

        self.redis.hincrby(self._metrics_key, "requests_served", 1)

        succeeded = False
        serialized = None
        try:
            # Resolve any Future IDs in the args before executing
            args = self._resolve_future_args(args)

            logger.info(
                "Executing %s.%s (future=%s) locally", service, function, future_id
            )
            result = method(**args)

            # Serialize the result
            if isinstance(result, (dict, list)):
                serialized = json.dumps(result)
            else:
                serialized = str(result)

            # Write result to local Redis
            self.redis.hset(f"future:{future_id}", "result", serialized)
            self.redis.hset(f"future:{future_id}", "failed", 0)
            succeeded = True

            # Push the result to any consumers registered on this node.
            self._fan_out_to_consumers(future_id, result=serialized)

            logger.info(
                "Completed %s.%s (future=%s) -> %s",
                service,
                function,
                future_id,
                serialized,
            )
        except Exception as e:
            logger.error("Failed to execute %s.%s: %s", service, function, e)

            self._mark_future_failed(future_id, e)
            self.redis.hincrby(self._metrics_key, "full_failures", 1)
        finally:
            wall_end = time.time()
            wall_duration = max(wall_end - wall_start, 0.0)
            cpu_seconds = max(time.thread_time() - thread_cpu_start, 0.0)
            cpu_percent = (
                (cpu_seconds / wall_duration * 100.0) if wall_duration else 0.0
            )
            gpu_percent = read_gpu_percent()

            self.redis.hset_multiple(
                f"future:{future_id}",
                {
                    "finished_at": wall_end,
                    "cpu_resource": cpu_percent,
                    "gpu_resource": gpu_percent,
                    "agent": self.agent_id,
                    **(
                        {"queue_time": max(wall_start - submitted_at, 0.0)}
                        if submitted_at is not None
                        else {}
                    ),
                },
            )

            # Send the completion callback only now that every final metric
            # has been written, so the snapshot sent to origin is complete.
            if origin and origin != self._my_endpoint:
                if succeeded:
                    self._send_result_callback(
                        origin, future_id, result=serialized, failed=0, error_message=""
                    )
                else:
                    error_message = self.redis.hget(f"future:{future_id}", "error")
                    self._send_result_callback(
                        origin, future_id, failed=1, error_message=error_message or ""
                    )

            ventis_context.set_current_future_id(parent or "")

    # ------------------------------------------------------------------ #
    #  Request forwarding                                                  #
    # ------------------------------------------------------------------ #

    def _get_remote_stub(self, endpoint):
        """Get or create a cached gRPC stub for a remote controller."""
        if endpoint not in self._remote_stubs:
            self._remote_channels[endpoint] = grpc.insecure_channel(
                endpoint, options=GRPC_CHANNEL_OPTIONS
            )
            self._remote_stubs[endpoint] = local_controler_pb2_grpc.LocalControllerStub(
                self._remote_channels[endpoint]
            )
            logger.info("Created gRPC connection to remote controller at %s", endpoint)
        return self._remote_stubs[endpoint]

    def _forward_request(self, endpoint, data):
        """Forward a request to a remote controller via gRPC."""
        # Tag the request with our endpoint so the remote LC can call back
        data["origin"] = self._my_endpoint
        # Pin the routing decision so the destination executes the request
        # instead of re-resolving it (which would let stateless requests
        # ping-pong between replicas).
        data["route_to"] = endpoint
        stub = self._get_remote_stub(endpoint)
        request = local_controler_pb2.JsonResponse(resonse=json.dumps(data))
        try:
            stub.Execute(request)
            logger.debug("Forwarded request to %s", endpoint)
        except Exception as e:
            logger.error("Failed to forward request to %s: %s", endpoint, e)
            self._mark_future_failed(data.get("future_id"), e)

    def _send_result_callback(
        self, origin, future_id, result="", failed=0, error_message=""
    ):
        """Send the future's full Redis hash to the originating controller."""
        if not result:
            logger.warning(
                "Agent '%s' is sending an empty/None result for future %s to origin %s, result: %s",
                self.agent_name,
                future_id,
                origin,
                result,
            )

        stub = self._get_remote_stub(origin)
        snapshot = self.redis.hgetall(f"future:{future_id}")
        snapshot.update(
            {
                "future_id": future_id,
                "result": result,
                "failed": int(bool(failed)),
                "error": str(error_message or ""),
            }
        )
        payload = json.dumps(snapshot)
        logger.info("Payload: Future %s,Sent %s ", future_id, payload)
        request = local_controler_pb2.JsonResponse(resonse=payload)
        try:
            stub.WriteResult(request)
            logger.info(
                "Sent result callback to %s for future %s, result %s",
                origin,
                future_id,
                result,
            )

        except Exception as e:
            logger.error("Failed to send result callback to %s: %s", origin, e)
            self._mark_future_failed(future_id, f"Result callback failed: {e}")

    def _fan_out_to_consumers(self, future_id, result=None, failed=0, error_message=""):
        """Push a completed future (result or failure) to every endpoint registered
        as a consumer on THIS node's Redis.

        Called at every site that writes a terminal value for a future -- local
        production (_execute_locally), failure (_mark_future_failed), and remote
        arrival (WriteResult) -- so propagation is event-driven and never depends
        on anyone calling Future.value(). Whichever node holds the consumer set is
        always either the producer or the origin, so one of those sites fires; the
        value then re-fans-out at each node it lands on, walking the graph.

        Delivery is intentionally not deduped: a consumer's WriteResult just
        re-writes the same value, so a redundant push (e.g. racing the immediate
        push in _process_request) is idempotent and harmless.
        """
        if not future_id:
            return
        for endpoint in self.redis.smembers(f"future:{future_id}:consumers"):
            if endpoint and endpoint != self._my_endpoint:
                self._send_result_callback(
                    endpoint,
                    future_id,
                    result=result,
                    failed=failed,
                    error_message=error_message,
                )

    # ------------------------------------------------------------------ #
    #  Shutdown                                                            #
    # ------------------------------------------------------------------ #

    def stop(self):
        """Gracefully shut down the server."""
        logger.info("Shutting down local controller...")
        self._metrics_stop_event.set()
        self._metrics_thread.join(timeout=2)
        self._executor.shutdown(wait=True)
        self.redis.set(self._status_key, "stopped")
        self.server.stop(0)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50051)
    args = parser.parse_args()

    controller = LocalController(port=args.port)
    controller.run()
