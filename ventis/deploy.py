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
import json
import logging
import os
import sys
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


def _routing_has_stateful_services(redis_client):
    """Return True when the routing table currently advertises any stateful service."""
    return bool(redis_client.hgetall("routing_table:stateful"))


def _configured_priority(redis_client, default=0):
    """Load this container's configured priority once, preferring env over Redis."""
    env_priority = os.environ.get("VENTIS_AGENT_PRIORITY")
    if env_priority is not None:
        return normalize_priority(env_priority, default)
    agent_name = os.environ.get("VENTIS_AGENT_NAME")
    if not agent_name:
        return default
    return normalize_priority(redis_client.hget(f"agent:{agent_name}:", "priority"), default)


def deploy(workflow_fn, port=8080, host="0.0.0.0", redis_host=None, redis_port=None):
    """
    Deploy a workflow function as a REST API endpoint.

    Creates a Flask server with:
        POST /<workflow_fn_name>  — accepts JSON args, returns {"request_id": "<id>"} plus
                                    {"agent_id": "<id>"} when stateful routing is in use (HTTP 202)
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
    workflow_priority = _configured_priority(redis_client, DEFAULT_AGENT_PRIORITY)

    fn_name = workflow_fn.__name__
    app = Flask(f"ventis-{fn_name}")

    def _execute_workflow(request_id, kwargs, context=None, agent_id=None):
        """Run the workflow in a background thread and store results in Redis."""
        status_key = f"request:{request_id}:status"
        result_key = f"request:{request_id}:result"
        error_key = f"request:{request_id}:error"
        context_key = f"request:{request_id}:context"

        try:
            redis_client.set(status_key, "running")
            logger.info("Executing workflow '%s' for request %s", fn_name, request_id)

            context = dict(context or {})
            session_priority, _, context = apply_effective_priorities(
                context=context,
                default_session=DEFAULT_SESSION_PRIORITY,
                default_agent=workflow_priority,
            )

            # Store context in Redis so Local Controllers can look it up
            if context:
                redis_client.set(context_key, json.dumps(context))

            # Set thread-local request ID so Futures spawned here carry it
            ventis_context.set_request_id(request_id)
            ventis_context.set_agent_id(agent_id)
            ventis_context.set_session_priority(session_priority)
            ventis_context.set_agent_priority(workflow_priority)

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
        finally:
            ventis_context.set_request_id(None)
            ventis_context.set_agent_id(None)
            ventis_context.set_session_priority(None)
            ventis_context.set_agent_priority(None)

    @app.route(f"/{fn_name}", methods=["POST"])
    def handle_workflow():
        """Accept a workflow request, dispatch async, return request ID."""
        # Parse request body as JSON args for the workflow function
        kwargs = request.get_json(force=True, silent=True) or {}

        # Extract policy context (if provided) before passing to workflow
        context = kwargs.pop("_context", {}) or {}
        context = dict(context)
        session_priority, _, context = apply_effective_priorities(
            context=context,
            default_session=DEFAULT_SESSION_PRIORITY,
            default_agent=workflow_priority,
        )
        agent_id = context.get("agent_id")
        if not agent_id and _routing_has_stateful_services(redis_client):
            agent_id = uuid.uuid4().hex
        if agent_id:
            context["agent_id"] = agent_id

        request_id = uuid.uuid4().hex
        status_key = f"request:{request_id}:status"
        redis_client.set(status_key, "pending")

        # Dispatch the workflow in a background thread
        thread = threading.Thread(
            target=_execute_workflow,
            args=(request_id, kwargs, context, agent_id),
            daemon=True,
        )
        thread.start()

        logger.info("Queued request %s for workflow '%s' with args: %s",
                     request_id, fn_name, kwargs)

        response = {"request_id": request_id}
        if agent_id:
            response["agent_id"] = agent_id
        return jsonify(response), 202

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

    logger.info("Deploying workflow '%s' at http://%s:%d/%s", fn_name, host, port, fn_name)
    logger.info("Status endpoint: GET http://%s:%d/status/<request_id>", host, port)

    app.run(host=host, port=port)
