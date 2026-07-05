DEFAULT_SESSION_PRIORITY = 0
DEFAULT_AGENT_PRIORITY = 0


def normalize_priority(value, default=0):
    """Return an int priority, falling back to default on missing/bad input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_effective_priorities(data=None, context=None, default_session=0, default_agent=0):
    """Resolve effective session/agent priorities from payload, context, and defaults."""
    data = data or {}
    context = context or {}
    session_priority = normalize_priority(
        data.get("session_priority", context.get("session_priority")),
        default_session,
    )
    agent_priority = normalize_priority(
        data.get("agent_priority", context.get("agent_priority")),
        default_agent,
    )
    return session_priority, agent_priority


def apply_effective_priorities(data=None, context=None, default_session=0, default_agent=0):
    """Write effective priorities back into payload/context and return them."""
    if data is None:
        data = {}
    if context is None:
        context = {}
    session_priority, agent_priority = extract_effective_priorities(
        data=data,
        context=context,
        default_session=default_session,
        default_agent=default_agent,
    )
    data["session_priority"] = session_priority
    data["agent_priority"] = agent_priority
    context["session_priority"] = session_priority
    context["agent_priority"] = agent_priority
    return session_priority, agent_priority, context
