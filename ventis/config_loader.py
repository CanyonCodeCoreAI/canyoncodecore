import os

import yaml


def _load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _agent_yaml_priority(project_root, agent_cfg):
    """Read priority from the matching agent YAML when available."""
    if agent_cfg.get("type") == "workflow":
        return None

    entrypoint = agent_cfg.get("entrypoint")
    if not entrypoint:
        return None

    yaml_path = os.path.splitext(os.path.join(project_root, entrypoint))[0] + ".yaml"
    if not os.path.isfile(yaml_path):
        return None

    agent_yaml = _load_yaml(yaml_path)
    agent_block = agent_yaml.get("agent", {})
    return agent_block.get("priority")


def load_config(config_path):
    """Load global config and fill agent priorities from agent YAML by default."""
    config = _load_yaml(config_path)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))

    for agent_cfg in config.get("agents", []):
        if "priority" in agent_cfg:
            continue
        priority = _agent_yaml_priority(project_root, agent_cfg)
        if priority is not None:
            agent_cfg["priority"] = priority

    return config
