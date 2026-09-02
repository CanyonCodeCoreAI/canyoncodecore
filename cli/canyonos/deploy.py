"""
Ventis Deploy Module

Provides `deploy()` to expose a workflow function as an async REST API endpoint.
Requests are assigned a unique ID and processed asynchronously. Results are
stored in Redis and can be polled via GET /status/<request_id>.

Usage:
    import ventis

    def my_workflow(query: str):
        finance = FinanceAgent()
        price = finance.get_stock_price(ticker=query)
        return {"price": price.value()}

    ventis.deploy(my_workflow, port=8080)
"""

try:
    import canyonos.ventis_context as ventis_context
except ImportError:
    import ventis_context
import json
import logging
import os
import threading
import time
import traceback
import uuid

from flask import Flask, request, jsonify
from werkzeug.serving import WSGIRequestHandler

# Try to import from absolute package (local install) or fallback to flat file (Docker container)
try:
    from canyonos.utils.redis_client import RedisClient
except ImportError:
    from redis_client import RedisClient

try:
    from canyonos.controller.utils.session_logging import get_session, upsert_session
except ImportError:
    from session_logging import get_session, upsert_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# How long a finished request's Redis keys stick around before Redis reclaims them.
COMPLETED_TTL_SECONDS = 300

# Written by GlobalController to every node's Redis.  This value changes when
# the controller reloads, so session operations must look it up live instead
# of relying solely on the environment captured when this process started.
IDENTITY_KEY = "controller:identity"

# session.status is a Postgres enum whose value set ("running"/"failed"/"completed")
# is owned by the database schema, not by us -- it cannot be renamed to match the
# /status API's vocabulary ("running"/"error"/"done"). Translate between them here.
_SESSION_STATUS_TO_REQUEST_STATUS = {
    "completed": "done",
    "failed": "error",
    "running": "running",
}


def _current_identity(redis_client, env_db_url, env_project_id):
    """Looks at the controller:identity field again if either project_id/db_url got updated

    Meant for when a new DB wants to be used on the same global controller (staging -> prod),
    Or if want to tag this workflow with a different project_id
    """
    identity = redis_client.hgetall(IDENTITY_KEY) or {}
    db_url = identity.get("database_url") or env_db_url
    project_id = identity.get("project_id") or env_project_id
    return db_url, project_id


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

    # These are fallbacks only. _current_identity() reads the controller's
    # current Redis value for every session transition and status fallback.
    env_db_url = os.environ.get("VENTIS_DATABASE_URL")
    env_project_id = os.environ.get("VENTIS_PROJECT_ID")

    fn_name = workflow_fn.__name__
    app = Flask(f"ventis-{fn_name}")

    def _expire_request_keys(request_id):
        """Let a finished request's Redis keys age out instead of living forever."""
        for suffix in ("status", "result", "error", "context"):
            redis_client.expire(
                f"request:{request_id}:{suffix}", COMPLETED_TTL_SECONDS
            )

    def _record_session(request_id, status, input_payload=None, output_payload=None):
        """Best-effort session upsert -- logs and swallows failures so a Postgres
        hiccup never takes down the request itself."""
        db_url, project_id = _current_identity(
            redis_client, env_db_url, env_project_id
        )
        if not (db_url and project_id):
            return
        try:
            upsert_session(
                db_url,
                project_id,
                request_id,
                status,
                time.time(),
                input_payload=input_payload,
                output_payload=output_payload,
            )
        except Exception as e:
            logger.warning(
                "Failed to update session %s status to %s (non-fatal): %s",
                request_id,
                status,
                e,
            )

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
            output_payload = result if isinstance(result, dict) else {"value": result}
            serialized = json.dumps(output_payload)

            redis_client.set(result_key, serialized)
            redis_client.set(status_key, "done")
            redis_client.sadd("request:completed", request_id)
            _expire_request_keys(request_id)
            logger.info("Request %s completed successfully.", request_id)

            _record_session(request_id, "completed", output_payload=output_payload)

        except Exception as e:
            logger.error("Request %s failed: %s", request_id, e)
            logger.error(traceback.format_exc())
            redis_client.set(error_key, str(e))
            redis_client.set(status_key, "error")
            redis_client.sadd("request:completed", request_id)
            _expire_request_keys(request_id)

            _record_session(request_id, "failed", output_payload={"error": str(e)})

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

        # Create the session row synchronously, before any Future for this request
        # can exist (the workflow dispatch below is what spawns those Futures), so
        # runtime_information's session_id foreign key can never race against it.
        _record_session(request_id, "running", input_payload=kwargs)

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

    def _status_from_session(request_id):
        """Rebuild a /status response from the session row in Postgres.

        Used once a finished request's Redis keys have expired. Returns None when
        there is nothing to serve, so the caller can fall through to its 404.
        """
        db_url, project_id = _current_identity(
            redis_client, env_db_url, env_project_id
        )
        if not (db_url and project_id):
            return None

        try:
            row = get_session(db_url, project_id, request_id)
            if row is None:
                return None

            status = _SESSION_STATUS_TO_REQUEST_STATUS.get(row["status"])
            if status is None:
                return None

            response = {"request_id": request_id, "status": status}
            output = row["output"]
            # JSONB comes back already decoded from Postgres, but as text from
            # drivers without a JSON type (e.g. sqlite in the tests).
            if isinstance(output, str):
                output = json.loads(output)

            if status == "done" and output:
                response["result"] = output
            elif status == "error" and isinstance(output, dict) and output.get("error"):
                response["error"] = output["error"]

            return response
        except Exception as e:
            logger.warning(
                "Failed to read session %s from Postgres (non-fatal): %s",
                request_id,
                e,
            )
            return None

    @app.route("/status/<request_id>", methods=["GET"])
    def get_status(request_id):
        """Check the status of a workflow request."""
        status_key = f"request:{request_id}:status"
        result_key = f"request:{request_id}:result"
        error_key = f"request:{request_id}:error"

        status = redis_client.get(status_key)
        if status is None:
            # Redis only holds finished requests for COMPLETED_TTL_SECONDS; after
            # that the session row is the record.
            from_session = _status_from_session(request_id)
            if from_session is not None:
                return jsonify(from_session), 200
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

    app.run(host=host, port=port, threaded=True, request_handler=type("_TimeoutWSGIRequestHandler", (WSGIRequestHandler,), {"timeout": 30}))
