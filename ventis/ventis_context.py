import threading

# Thread-local storage for request context
_local = threading.local()


def set_request_id(request_id: str):
    """Set the current request ID for this thread."""
    _local.request_id = request_id


def get_request_id() -> str:
    """Get the current request ID for this thread, or an empty string if not set."""
    return getattr(_local, "request_id", "")


def set_workflow_name(name: str):
    """Set the current workflow name for this thread."""
    _local.workflow_name = name


def get_workflow_name() -> str:
    """Get the current workflow name for this thread, or an empty string if not set."""
    return getattr(_local, "workflow_name", "")
