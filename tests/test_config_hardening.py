from pathlib import Path

import pytest
import yaml

from canyonos_core.cli import _load_config


def _write_config(tmp_path, agents):
    path = Path(tmp_path, "global_controller.yaml")
    path.write_text(yaml.safe_dump({"agents": agents}))
    return path


@pytest.mark.parametrize("replicas", [0, -1, 1.5, "2", [], True])
def test_replicas_must_be_a_positive_integer(tmp_path, replicas):
    path = _write_config(
        tmp_path,
        [{"name": "Agent", "entrypoint": "agent.py", "replicas": replicas}],
    )

    with pytest.raises(
        RuntimeError, match="positive integer.*replicas|replicas.*positive integer"
    ):
        _load_config(path)


def test_unknown_provider_is_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        [{"name": "Agent", "entrypoint": "agent.py", "provider": "locla"}],
    )

    with pytest.raises(RuntimeError, match="unsupported provider"):
        _load_config(path)


def test_provider_case_is_normalized(tmp_path):
    path = _write_config(
        tmp_path,
        [{"name": "Agent", "entrypoint": "agent.py", "provider": "LOCAL"}],
    )

    assert _load_config(path)["agents"][0]["provider"] == "local"


def test_case_colliding_names_are_rejected(tmp_path):
    path = _write_config(
        tmp_path,
        [
            {"name": "Agent", "entrypoint": "agent.py"},
            {"name": "agent", "entrypoint": "other.py"},
        ],
    )

    with pytest.raises(RuntimeError, match="Duplicate agent names"):
        _load_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_port", 0), ("redis_port", 65536), ("host_port", "8000")],
)
def test_ports_are_validated(tmp_path, field, value):
    path = _write_config(
        tmp_path,
        [{"name": "Workflow", "workflow_file": "workflow.py", field: value}],
    )

    with pytest.raises(RuntimeError, match=field):
        _load_config(path)


def test_multiple_local_workflow_replicas_fail_with_an_actionable_error(tmp_path):
    path = _write_config(
        tmp_path,
        [
            {
                "name": "Workflow",
                "type": "workflow",
                "workflow_file": "workflow.py",
                "provider": "local",
                "replicas": 2,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="same `api_port`"):
        _load_config(path)
