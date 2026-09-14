"""Docker container names for agent replicas.

Every place that creates, bootstraps, or cleans up a replica container derives
the name from here, so the provider runtimes and the controller's stale-container
cleanup cannot drift apart.
"""


def container_name(agent_name, replica_index):
    return f"canyonos-{agent_name.lower()}-{replica_index}"
