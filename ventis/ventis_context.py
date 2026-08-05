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


def set_current_metrics_key(metrics_key: str):
    """Set the Redis metrics-hash key of the controller instance currently executing on this thread."""
    _local.current_metrics_key = metrics_key


def get_current_metrics_key() -> str:
    """Get the current metrics-hash key, or an empty string if not set."""
    return getattr(_local, "current_metrics_key", "")
