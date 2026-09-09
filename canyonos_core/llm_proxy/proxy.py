"""Auto-inject CanyonOS headers into ALL boto3 Bedrock calls.

Import this module once and all subsequent boto3.client("bedrock-runtime") calls
will automatically include the X-Canyonos-Future-ID header.

Usage:
    import canyonos_core.llm_proxy_auto  # Just import once
    import boto3
    
    # Now this automatically includes the header!
    client = boto3.client("bedrock-runtime")
    response = client.converse(...)
"""

import os

import boto3
import logging

# Test/stub mode: when CANYONOS_LLM_STUB_TEXT is set, the LLM proxy returns
# canned text and NEVER calls AWS. boto3 still needs *some* credentials to
# compute a local SigV4 signature for the request it sends to the local proxy
# endpoint (AWS_ENDPOINT_URL_BEDROCK_RUNTIME -> 127.0.0.1:8081), so supply
# throwaway ones here. The signed request goes only to the local proxy; these
# credentials are never transmitted to AWS. This makes a stubbed e2e run need
# no real AWS credentials at all.
if os.getenv("CANYONOS_LLM_STUB_TEXT"):
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "stub")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "stub")
    os.environ.setdefault(
        "AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "us-east-1")
    )

try:
    import canyonos_core.controller.canyonos_context as canyonos_context
except ImportError:
    # In-container the framework files are copied flat to /app.
    try:
        import canyonos_context
    except ImportError:
        canyonos_context = None

log = logging.getLogger(__name__)


def _inject_canyonos_headers(params=None, **kwargs):
    """Inject X-Canyonos-Future-ID into the outgoing Bedrock HTTP request.

    Registered on boto3's ``before-call.bedrock-runtime`` event, whose handlers
    receive the prepared-request ``params`` dict (with a mutable ``headers``).
    The ``request`` object only exists on the later ``before-send`` event, so
    reading it here would always be None and silently drop the header.
    """
    if not canyonos_context or params is None:
        return

    # Get current future_id from thread-local context
    try:
        future_id = canyonos_context.get_current_future_id()
        if future_id:
            params.setdefault("headers", {})["X-Canyonos-Future-ID"] = future_id
            log.debug("Injected X-Canyonos-Future-ID: %s", future_id)
    except Exception as e:
        log.debug("Could not inject future_id: %s", e)


# Register the hook globally on the default session
_session = boto3.Session()
_session.events.register_first('before-call.bedrock-runtime', _inject_canyonos_headers)

# Also patch the default session used by boto3.client()
boto3.DEFAULT_SESSION = _session

log.info("CanyonOS boto3 hook registered - all Bedrock calls will include future_id header")
