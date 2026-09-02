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
    import ventis.ventis_context as ventis_context
except ImportError:
    ventis_context = None

log = logging.getLogger(__name__)


def _inject_ventis_headers(event_name=None, **kwargs):
    """Inject X-Ventis-Future-ID header into boto3 requests."""
    if not ventis_context:
        return
    
    # Only inject for bedrock-runtime service
    if 'service_id' in kwargs and kwargs.get('service_id') != 'Bedrock Runtime':
        return
    
    # Get the request object
    request = kwargs.get('request')
    if not request:
        return
    
    # Get current future_id from thread-local context
    try:
        future_id = ventis_context.get_current_future_id()
        if future_id:
            request.headers['X-Ventis-Future-ID'] = future_id
            log.debug("Injected X-Ventis-Future-ID: %s", future_id)
    except Exception as e:
        log.debug("Could not inject future_id: %s", e)


# Register the hook globally on the default session
_session = boto3.Session()
_session.events.register_first('before-call.bedrock-runtime', _inject_ventis_headers)

# Also patch the default session used by boto3.client()
boto3.DEFAULT_SESSION = _session

log.info("Ventis boto3 hook registered - all Bedrock calls will include future_id header")
