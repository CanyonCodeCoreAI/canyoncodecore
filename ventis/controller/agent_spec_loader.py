import json

import yaml


def write_agent_specs(config_path, redis_client):
    """Read global_controller.yaml and write agent specs to Redis."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}

    for agent in config.get("agents", []):
        name = agent["name"]
        spec_class = "WorkflowSpec" if agent.get("type") == "workflow" else "AgentSpec"
        redis_client.hset_multiple(
            f"agent:{name}:",
            {
                "class": spec_class,
                "resources": json.dumps(agent.get("resources", {})),
                "replicas": json.dumps(agent.get("replicas", 1)),
                "stateful": json.dumps(agent.get("stateful", False)),
                "redis_port": str(agent.get("redis_port", 6379)),
                "provider": agent.get("provider", "local"),
                "priority": str(agent.get("priority", 0)),
            },
        )
