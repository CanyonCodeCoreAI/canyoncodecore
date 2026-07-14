import time
import json
import uuid
import sys
import os
import logging

import grpc

try:
    import ventis.ventis_context as ventis_context
except ImportError:
    import ventis_context

# Add generated grpc_stubs to path (Docker context copies them directly to /app, and local relies on project dir)
sys.path.insert(0, ".")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath("grpc_stubs"))

try:
    from ventis.utils.redis_client import RedisClient
except ImportError:
    from redis_client import RedisClient
import local_controler_pb2
import local_controler_pb2_grpc

logger = logging.getLogger(__name__)


# defines the future object which will be returned by each function call
class Future(object):
    redis = RedisClient(
        host=os.environ.get("VENTIS_REDIS_HOST", "localhost"),
        port=int(os.environ.get("VENTIS_REDIS_PORT", 6379)),
    )

    # Single local controller connection, shared across all futures
    _lc_host = os.environ.get("VENTIS_LC_HOST", "localhost")
    _lc_port = os.environ.get("VENTIS_LC_PORT", "50051")
    _channel = None
    _stub = None

    @classmethod
    def _get_stub(cls):
        """Get or create the cached gRPC stub for the local controller."""
        if cls._stub is None:
            endpoint = f"{cls._lc_host}:{cls._lc_port}"
            cls._channel = grpc.insecure_channel(endpoint)
            cls._stub = local_controler_pb2_grpc.LocalControllerStub(cls._channel)
            logger.info("Connected to local controller at %s", endpoint)
        return cls._stub

    def __init__(self, parent, service, method, args=None):
        """
        parent: parent future
        service: service to be called
        method: method to be called in service
        args: arguments to be passed to the method
        """

        # initial value of future object
        self.id = uuid.uuid4().hex

        # Grab the request_id from the thread-local context (set by deploy)
        self.request_id = ventis_context.get_request_id()
        self.workflow = ventis_context.get_workflow_name()

        # this provides the funtionality we need to execute
        self.funtionality = None
        self.executor = None
        self.result = None
        self.parent = parent
        self.service = service
        self.method = method
        self.args = args or {}
        self.children = []
        self.consumers = []
        # For simplicity I am making a decision here, the future value only be sent back to the parent
        # Store scalar fields in a Redis Hash: future:<id>
        self.redis.hset_multiple(
            self._key(),
            {
                "id": self.id,
                "request_id": self.request_id or "",
                "workflow": self.workflow or "",
                "result": "",
                "parent": self.parent,
                "service": self.service,
                "method": self.method,
                "args": json.dumps(self.args),
            },
        )
        # Register this future under its request so local controllers can clean it up
        if self.request_id:
            self.redis.sadd(f"request:{self.request_id}:futures", self.id)
        # If a future is calculated, this flag will be true
        self.calculated = False
        # Submit the request to the local controller
        self._submit_request()

    def _submit_request(self):
        """Send the gRPC request to the local controller."""
        stub = self._get_stub()
        request_payload = json.dumps(
            {
                "service": self.service,
                "function": self.method,
                "args": self.args,
                "future_id": self.id,
                "request_id": self.request_id,
                "workflow": self.workflow,
            }
        )
        request = local_controler_pb2.JsonResponse(resonse=request_payload)
        try:
            self.response = stub.Execute(request)
            logger.debug(
                "Submitted %s.%s (future=%s)", self.service, self.method, self.id
            )
        except Exception as e:
            logger.error("gRPC call failed for %s.%s: %s", self.service, self.method, e)
            raise

    def _key(self):
        """Redis key for this future's hash."""
        return f"future:{self.id}"

    def _children_key(self):
        """Redis key for this future's children set."""
        return f"future:{self.id}:children"

    def _consumers_key(self):
        """Redis key for this future's consumers set."""
        return f"future:{self.id}:consumers"

    def _poll_redis(self):
        """Check Redis for a computed result and cache it locally."""
        error = self.redis.hget(self._key(), "error")
        if error:
            raise RuntimeError(error)
        result = self.redis.hget(self._key(), "result")
        if result is not None and result != "":
            self.result = result

        return self.result

    def value(self, timeout=None):
        """
        This method will block until the value is computed or the timeout is reached.
        Returns immediately if the result is already available locally.
        Polls Redis periodically to check for computed results.
        """
        if self.result is not None:
            return self.result

        if timeout is None:
            while self._poll_redis() is None:
                time.sleep(0.01)
        else:
            start_time = time.time()
            while self._poll_redis() is None and time.time() - start_time < timeout:
                time.sleep(0.01)
            if self.result is None:
                raise TimeoutError(
                    f"Future value not available within {timeout} seconds"
                )
        self.calculated = True

        # Push result to all consumers
        self._notify_consumers()

        return self.result

    # def __call__(self, timeout=None):
    #     """Allow calling the future directly to get the value, e.g. future()."""
    #     return self.value(timeout)

    def is_available(self):
        """
        This method will return True if the value is computed, False otherwise.
        """
        return self.result is not None

    def _get_consumers(self):
        """Return the list of consumers from Redis."""
        return self.redis.smembers(self._consumers_key())

    def _notify_consumers(self):
        """Push this future's result to all registered consumer endpoints via gRPC WriteResult."""
        consumers = self._get_consumers()
        if not consumers:
            return
        for endpoint in consumers:
            try:
                if not self.result:
                    logger.warning(
                        "Future %s is notifying consumer %s with an empty/None result",
                        self.id,
                        endpoint,
                    )
                channel = grpc.insecure_channel(endpoint)
                stub = local_controler_pb2_grpc.LocalControllerStub(channel)
                payload = json.dumps({"future_id": self.id, "result": self.result})
                request = local_controler_pb2.JsonResponse(resonse=payload)
                stub.WriteResult(request)
                logger.info(
                    "Notified consumer %s with result for future %s", endpoint, self.id
                )
            except Exception as e:
                logger.error(
                    "Failed to notify consumer %s for future %s: %s",
                    endpoint,
                    self.id,
                    e,
                )

    def _add_consumer(self, consumer):
        """Add a consumer."""
        self.consumers.append(consumer)
        self.redis.sadd(self._consumers_key(), consumer)

    def _remove_consumer(self, consumer):
        """Remove a consumer."""
        self.consumers.remove(consumer)
        self.redis.srem(self._consumers_key(), consumer)
