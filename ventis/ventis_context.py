import threading

# Thread-local storage for request context
_local = threading.local()


def set_request_id(request_id: str):
    """Set the current request ID for this thread."""
    _local.request_id = request_id


def get_request_id() -> str:
    """Get the current request ID for this thread, or None if not set."""
    return getattr(_local, "request_id", "")
