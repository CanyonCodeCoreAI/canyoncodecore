"""Auto-inject Ventis headers into ALL boto3 Bedrock calls.

Import this module once and all subsequent boto3.client("bedrock-runtime") calls
will automatically include the X-Ventis-Future-ID header.

Usage:
    import ventis.llm_proxy_auto  # Just import once
    import boto3
    
    # Now this automatically includes the header!
    client = boto3.client("bedrock-runtime")
    response = client.converse(...)
"""

import boto3
import logging

try:
    import ventis.controller.ventis_context as ventis_context
except ImportError:
    # In-container the framework files are copied flat to /app.
    try:
        import ventis_context
    except ImportError:
        ventis_context = None

log = logging.getLogger(__name__)


def _inject_ventis_headers(params=None, **kwargs):
    """Inject X-Ventis-Future-ID into the outgoing Bedrock HTTP request.

    Registered on boto3's ``before-call.bedrock-runtime`` event, whose handlers
    receive the prepared-request ``params`` dict (with a mutable ``headers``).
    The ``request`` object only exists on the later ``before-send`` event, so
    reading it here would always be None and silently drop the header.
    """
    if not ventis_context or params is None:
        return

    # Get current future_id from thread-local context
    try:
        future_id = ventis_context.get_current_future_id()
        if future_id:
            params.setdefault("headers", {})["X-Ventis-Future-ID"] = future_id
            log.debug("Injected X-Ventis-Future-ID: %s", future_id)
    except Exception as e:
        log.debug("Could not inject future_id: %s", e)


# Register the hook globally on the default session
_session = boto3.Session()
_session.events.register_first('before-call.bedrock-runtime', _inject_ventis_headers)

# Also patch the default session used by boto3.client()
boto3.DEFAULT_SESSION = _session

log.info("Ventis boto3 hook registered - all Bedrock calls will include future_id header")
