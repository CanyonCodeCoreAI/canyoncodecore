import threading

# Thread-local storage for request context
_local = threading.local()

def set_request_id(request_id: str):
    """Set the current request ID for this thread."""
    _local.request_id = request_id

def get_request_id() -> str:
    """Get the current request ID for this thread, or None if not set."""
    return getattr(_local, "request_id", None)

def set_agent_id(agent_id: str):
    """Set the current agent ID for this thread."""
    _local.agent_id = agent_id

def get_agent_id() -> str:
    """Get the current agent ID for this thread, or None if not set."""
    return getattr(_local, "agent_id", None)


def set_session_priority(session_priority: int):
    """Set the current session priority for this thread."""
    _local.session_priority = session_priority


def get_session_priority() -> int:
    """Get the current session priority for this thread, or None if not set."""
    return getattr(_local, "session_priority", None)


def set_agent_priority(agent_priority: int):
    """Set the current caller agent priority for this thread."""
    _local.agent_priority = agent_priority


def get_agent_priority() -> int:
    """Get the current caller agent priority for this thread, or None if not set."""
    return getattr(_local, "agent_priority", None)
