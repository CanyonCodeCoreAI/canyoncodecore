import threading

# Thread-local storage for request context
_local = threading.local()


def set_request_id(request_id: str):
    """Set the current request ID for this thread."""
    _local.request_id = request_id


def get_request_id() -> str:
    """Get the current request ID for this thread, or an empty string if not set."""
    return getattr(_local, "request_id", "")


def set_current_future_id(future_id: str):
    """Set the future_id currently executing on this thread."""
    _local.current_future_id = future_id


def get_current_future_id() -> str:
    """Get the future_id currently executing on this thread, or an empty string if not set."""
    return getattr(_local, "current_future_id", "")


# Future ids are 64-bit so they can double as the OTel span_id, which the spec
# fixes at 64 bits (see OTLP_Exporter/convert.py, which feeds Future.id straight
# into SpanContext without truncating). Widening this breaks trace export, so the
# generator and every "is this arg a future id?" check read the width from here.
FUTURE_ID_BYTES = 8
FUTURE_ID_HEX_LENGTH = FUTURE_ID_BYTES * 2

_HEX_DIGITS = "0123456789abcdefABCDEF"


def looks_like_future_id(value) -> bool:
    """True if value has the shape of a Future.id, i.e. a future-id-width hex string."""
    return (
        isinstance(value, str)
        and len(value) == FUTURE_ID_HEX_LENGTH
        and all(c in _HEX_DIGITS for c in value)
    )


def set_current_metrics_key(metrics_key: str):
    """Set the Redis metrics-hash key of the controller instance currently executing on this thread."""
    _local.current_metrics_key = metrics_key


def get_current_metrics_key() -> str:
    """Get the current metrics-hash key, or an empty string if not set."""
    return getattr(_local, "current_metrics_key", "")
