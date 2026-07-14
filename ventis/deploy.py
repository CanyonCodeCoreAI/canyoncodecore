"""
Ventis Deploy Module

Provides `deploy()` to expose a workflow function as an async REST API endpoint.
Requests are assigned a unique ID and processed asynchronously. Results are
stored in Redis and can be polled via GET /status/<request_id>.

Usage:
    import ventis

    def my_workflow(query: str):
        finance = FinanceAgentStub()
        price = finance.get_stock_price(ticker=query)
        return {"price": price.value()}

    ventis.deploy(my_workflow, port=8080)
"""

try:
    import ventis.ventis_context as ventis_context
except ImportError:
    import ventis_context
import json
import logging
import os
import threading
import traceback
import uuid

from flask import Flask, request, jsonify

# Try to import from absolute package (local install) or fallback to flat file (Docker container)
try:
    from ventis.utils.redis_client import RedisClient
except ImportError:
    from redis_client import RedisClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def deploy(workflow_fn, port=8080, host="0.0.0.0", redis_host=None, redis_port=None):
    """
    Deploy a workflow function as a REST API endpoint.

    Creates a Flask server with:
        POST /<workflow_fn_name>  — accepts JSON args, returns {"request_id": "<id>"} (HTTP 202)
        GET  /status/<request_id> — returns status and result

    Args:
        workflow_fn:  The workflow function to expose.
        port:         Port for the REST server (default: 8080).
        host:         Host to bind to (default: 0.0.0.0).
        redis_host:   Redis host (default: from env or localhost).
        redis_port:   Redis port (default: from env or 6379).
    """
    redis_host = redis_host or os.environ.get("VENTIS_REDIS_HOST", "localhost")
    redis_port = redis_port or int(os.environ.get("VENTIS_REDIS_PORT", 6379))
    redis_client = RedisClient(host=redis_host, port=redis_port)

    fn_name = workflow_fn.__name__
    app = Flask(f"ventis-{fn_name}")

    def _execute_workflow(request_id, kwargs, context=None):
        """Run the workflow in a background thread and store results in Redis."""
        status_key = f"request:{request_id}:status"
        result_key = f"request:{request_id}:result"
        error_key = f"request:{request_id}:error"
        context_key = f"request:{request_id}:context"

        try:
            redis_client.set(status_key, "running")
            logger.info("Executing workflow '%s' for request %s", fn_name, request_id)

            # Store context in Redis so Local Controllers can look it up
            if context:
                redis_client.set(context_key, json.dumps(context))

            # Set thread-local request ID so Futures spawned here carry it
            ventis_context.set_request_id(request_id)

            result = workflow_fn(**kwargs)

            # Serialize the result
            if isinstance(result, dict):
                serialized = json.dumps(result)
            else:
                serialized = json.dumps({"value": result})

            redis_client.set(result_key, serialized)
            redis_client.set(status_key, "done")
            redis_client.sadd("request:completed", request_id)
            logger.info("Request %s completed successfully.", request_id)

        except Exception as e:
            logger.error("Request %s failed: %s", request_id, e)
            logger.error(traceback.format_exc())
            redis_client.set(error_key, str(e))
            redis_client.set(status_key, "error")
            redis_client.sadd("request:completed", request_id)

    @app.route(f"/{fn_name}", methods=["POST"])
    def handle_workflow():
        """Accept a workflow request, dispatch async, return request ID."""
        # Parse request body as JSON args for the workflow function
        kwargs = request.get_json(force=True, silent=True) or {}

        # Extract policy context (if provided) before passing to workflow
        context = kwargs.pop("_context", {})

        request_id = uuid.uuid4().hex
        status_key = f"request:{request_id}:status"
        redis_client.set(status_key, "pending")
        redis_client.set(f"request:{request_id}:workflow", fn_name)


        # Dispatch the workflow in a background thread
        thread = threading.Thread(
            target=_execute_workflow,
            args=(request_id, kwargs, context),
            daemon=True,
        )
        thread.start()

        logger.info(
            "Queued request %s for workflow '%s' with args: %s",
            request_id,
            fn_name,
            kwargs,
        )

        return jsonify({"request_id": request_id}), 202

    @app.route("/status/<request_id>", methods=["GET"])
    def get_status(request_id):
        """Check the status of a workflow request."""
        status_key = f"request:{request_id}:status"
        result_key = f"request:{request_id}:result"
        error_key = f"request:{request_id}:error"

        status = redis_client.get(status_key)
        if status is None:
            return jsonify({"error": "Request not found"}), 404

        response = {"request_id": request_id, "status": status}

        if status == "done":
            result = redis_client.get(result_key)
            if result:
                response["result"] = json.loads(result)

        elif status == "error":
            error = redis_client.get(error_key)
            if error:
                response["error"] = error

        return jsonify(response), 200

    logger.info(
        "Deploying workflow '%s' at http://%s:%d/%s", fn_name, host, port, fn_name
    )
    logger.info("Status endpoint: GET http://%s:%d/status/<request_id>", host, port)

    app.run(host=host, port=port)
