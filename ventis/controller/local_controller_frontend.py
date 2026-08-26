# Local Controller Frontend - gRPC Server
# Accepts incoming Execute requests and pushes them into a Python queue for processing.

import grpc
from concurrent import futures
import os
from threading import Thread
import json
import queue
import logging
import sys

# Add generated grpc_stubs to the path (Docker context copies them directly to /app)
sys.path.insert(0, ".")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath("grpc_stubs"))

import local_controler_pb2
import local_controler_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LocalControllerServicer(local_controler_pb2_grpc.LocalControllerServicer):
    """gRPC servicer that accepts requests and pushes them into a queue."""

    def __init__(self, my_endpoint="unknown"):
        self.request_queue = queue.Queue()
        self.my_endpoint = my_endpoint
        # Redis client for writing results back to local Redis
        redis_host = os.environ.get("VENTIS_REDIS_HOST", "localhost")
        redis_port = int(os.environ.get("VENTIS_REDIS_PORT", 6379))
        try:
            from ventis.utils.redis_client import RedisClient
        except ImportError:
            from redis_client import RedisClient
        self.redis = RedisClient(host=redis_host, port=redis_port)
        # Set by LocalController: fans a just-arrived result out to this node's
        # consumers. Signature: on_result(future_id, result, failed, error_message).
        self.on_result = None

    def Execute(self, request, context):
        """Accept an Execute request and push it into the queue."""
        logger.info(f"Received request: {request.resonse}")
        data = json.loads(request.resonse)
        self.request_queue.put(request.resonse)
        return local_controler_pb2.JsonResponse(resonse="Request queued successfully")

    def WriteResult(self, request, context):
        """Accept a result or error from a remote controller and write it to local Redis."""
        try:
            data = json.loads(request.resonse)
            future_id = data.get("future_id")
            result = data.get("result")
            failed = int(bool(data.get("failed", 0)))

            logger.info(
                f"WriteResult: received result for future {future_id}: {result}"
            )
            if not result:
                logger.warning(
                    f"WriteResult received empty/None result for future {future_id} from {context.peer()}"
                )

            if future_id:
                if data:
                    self.redis.hset_multiple(f"future:{future_id}", data)
                if failed:
                    logger.info("WriteResult: wrote error for future %s", future_id)
                elif result is not None:
                    logger.info(
                        "WriteResult: wrote result for future %s, result %s",
                        future_id,
                        result,
                    )
                # Relay the just-arrived value to any consumers registered on
                # this node (the origin is where consumer sets live). This is
                # what walks the value hop-by-hop through the graph.
                if self.on_result:
                    self.on_result(
                        future_id,
                        result=result,
                        failed=failed,
                        error_message=error_message,
                    )
            else:
                logger.error("WriteResult: missing future_id in %s", data)
        except Exception as e:
            logger.error("WriteResult failed: %s", e)
        return local_controler_pb2.JsonResponse(resonse="Result written")

    def Cleanup(self, request, context):
        """Trigger async cleanup for one or more completed requests."""
        try:
            data = json.loads(request.resonse)
            request_ids = data.get("request_ids")

            if request_ids:
                # Process the cleanup batch asynchronously so the RPC returns immediately.
                def _cleanup_batch():
                    for request_id in request_ids:
                        self._cleanup_request(request_id)

                Thread(target=_cleanup_batch, daemon=True).start()
            else:
                logger.warning("Cleanup: missing request_id(s) in payload")
        except Exception as e:
            logger.error("Cleanup: failed to parse payload: %s", e)
        return local_controler_pb2.JsonResponse(resonse="Cleanup triggered")

    def _cleanup_request(self, request_id):
        """Delete a request's consolidated future hashes and bookkeeping."""
        # Atomically claim cleanup — prevents duplicate work when multiple LCs share a Redis
        lock_key = f"request:{request_id}:cleanup_lock"
        if not self.redis.setnx(lock_key, self.my_endpoint):
            logger.info(
                "Cleanup for request %s already claimed by another LC, skipping.",
                request_id,
            )
            return

        try:
            futures_key = f"request:{request_id}:futures"
            future_ids = self.redis.smembers(futures_key)
            if not future_ids:
                logger.info("No futures found for request %s on this node.", request_id)
                return

            keys_to_delete = [futures_key]
            for fid in future_ids:
                keys_to_delete.extend(
                    [
                        f"future:{fid}",
                        f"future:{fid}:children",
                        f"future:{fid}:consumers",
                    ]
                )
            self.redis.delete(*keys_to_delete)
            logger.info(
                "Cleaned up %d future(s) for request %s", len(future_ids), request_id
            )

            # Clean up affinity bindings for this request
            self.redis.delete(f"affinity:{request_id}")
            logger.info("Cleaned up affinity bindings for request %s", request_id)
        finally:
            # Always release the lock, even if cleanup partially failed
            self.redis.delete(lock_key)


def start_server(port=50051, my_endpoint="unknown"):
    """Start the gRPC server."""
    try:
        from ventis.utils.grpc_options import GRPC_SERVER_OPTIONS
    except ImportError:
        from grpc_options import GRPC_SERVER_OPTIONS

    servicer = LocalControllerServicer(my_endpoint=my_endpoint)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=1), options=GRPC_SERVER_OPTIONS
    )
    local_controler_pb2_grpc.add_LocalControllerServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"Local controller frontend started on port {port}")

    return server, servicer


if __name__ == "__main__":
    server, request_queue = start_server()
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.stop(0)
