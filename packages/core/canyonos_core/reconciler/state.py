# Reconciler State
# The durable reconciliation schema in Redis, and the fire-and-forget API that
# writes to it. Plain functions over a RedisClient so any process can call them.
#
# Schema:
#   agent:{name}:desired_replicas  int    desired replica count (the desired state)
#   reconciler:wake                list   wake signals; payload is an agent name or "*"
#   reconciler:reap                set    instance ids to destroy on the next pass
#
# Observed state stays where InstanceManager already writes it
# (agent:{name}:instances, agent_instance:{id}).

import logging

logger = logging.getLogger(__name__)

WAKE_QUEUE_KEY = "reconciler:wake"
REAP_SET_KEY = "reconciler:reap"
WAKE_ALL = "*"

# One drain must not spin forever on a queue being written to concurrently.
_DRAIN_LIMIT = 1000


def desired_key(agent_name):
    return f"agent:{agent_name}:desired_replicas"


# ---------------------------------------------------------------------- #
#  Desired state                                                         #
# ---------------------------------------------------------------------- #


def get_desired(redis_client, agent_name, default=0):
    """Desired replica count for an agent, or default if it was never recorded."""
    raw = redis_client.get(desired_key(agent_name))
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-integer desired_replicas %r for agent %s", raw, agent_name
        )
        return default


def set_desired(redis_client, agent_name, count):
    """Set an agent's desired replica count outright."""
    count = max(0, int(count))
    redis_client.set(desired_key(agent_name), count)
    return count


def seed_desired(redis_client, agent_specs):
    """
    Record each agent's configured replica count, leaving any existing value alone.

    Redis is authoritative once written, so a scale applied at runtime survives a
    controller restart instead of being reverted to whatever the YAML still says.
    """
    for spec in agent_specs:
        name = spec["name"]
        replicas = spec.get("replicas", 1)
        if not isinstance(replicas, int):
            logger.warning(
                "Agent %s declares a non-integer replicas value (%r); "
                "reconciliation needs a count, skipping it.",
                name,
                replicas,
            )
            continue
        if redis_client.get(desired_key(name)) is None:
            set_desired(redis_client, name, replicas)


def scale(redis_client, agent_name, delta):
    """Move an agent's desired replica count by delta. Returns the new count."""
    new_count = redis_client.incrby(desired_key(agent_name), int(delta))
    if new_count < 0:
        return set_desired(redis_client, agent_name, 0)
    return new_count


def desired_agent_specs(redis_client, agent_specs):
    """
    The full agent spec list with each spec's replicas replaced by its desired count.

    Always reconcile against the whole list: InstanceManager.ensure_instances
    republishes the routing snapshot from the specs it is handed and drops every
    service missing from them. A spec whose replicas is not a count is passed
    through untouched rather than dropped, so it still fails where it always has
    instead of silently disappearing from the routing table.
    """
    specs = []
    for spec in agent_specs:
        configured = spec.get("replicas", 1)
        if not isinstance(configured, int):
            specs.append(spec)
            continue
        specs.append(
            {**spec, "replicas": get_desired(redis_client, spec["name"], configured)}
        )
    return specs


# ---------------------------------------------------------------------- #
#  Wake queue                                                            #
# ---------------------------------------------------------------------- #


def request_reconcile(redis_client, agent_name=WAKE_ALL):
    """Ask the reconciler to converge an agent (or everything) as soon as it can."""
    redis_client.lpush(WAKE_QUEUE_KEY, agent_name)


def drain(redis_client, timeout=1):
    """
    Wait for wake signals and collect every one currently queued.

    Blocks up to timeout seconds for the first signal, then takes the rest without
    blocking, so a burst of identical signals collapses into a single pass.
    """
    first = redis_client.brpop(WAKE_QUEUE_KEY, timeout=timeout)
    if first is None:
        return set()

    signals = {first}
    for _ in range(_DRAIN_LIMIT):
        signal = redis_client.rpop(WAKE_QUEUE_KEY)
        if signal is None:
            break
        signals.add(signal)
    return signals


# ---------------------------------------------------------------------- #
#  Targeted replacement                                                  #
# ---------------------------------------------------------------------- #


def request_replace(redis_client, instance_id):
    """Mark one instance to be destroyed; the loop refills its slot afterwards."""
    redis_client.sadd(REAP_SET_KEY, instance_id)


def take_reap_requests(redis_client, agent_name):
    """
    Claim the pending reap requests belonging to an agent.

    Claimed ids are removed up front: a crash mid-pass loses the request rather
    than replacing the instance again on every future pass.
    """
    prefix = f":{agent_name}:"
    claimed = {
        instance_id
        for instance_id in redis_client.smembers(REAP_SET_KEY)
        if prefix in instance_id
    }
    if claimed:
        redis_client.srem(REAP_SET_KEY, *claimed)
    return claimed
