"""Lazily-cached lookups over the static aws_instance_pricing.db reference data."""

import os

from sqlalchemy import create_engine, text

_PRICING_DB_PATH = os.path.join(os.path.dirname(__file__), "aws_instance_pricing.db")

_hourly_cost_by_instance_type = None
_token_cost_by_model_id = None


def _load_cache():
    global _hourly_cost_by_instance_type, _token_cost_by_model_id
    if _hourly_cost_by_instance_type is not None:
        return

    engine = create_engine(f"sqlite:///{_PRICING_DB_PATH}")
    with engine.connect() as conn:
        instance_rows = conn.execute(
            text("SELECT instance_type, hourly_cost FROM aws_instance_pricing")
        ).fetchall()
        model_rows = conn.execute(
            text(
                "SELECT model_id, input_cost_per_million_tokens, "
                "output_cost_per_million_tokens FROM bedrock_model_pricing"
            )
        ).fetchall()
    engine.dispose()

    _hourly_cost_by_instance_type = {row[0]: row[1] for row in instance_rows}
    _token_cost_by_model_id = {row[0]: (row[1], row[2]) for row in model_rows}


def get_hourly_cost(instance_type):
    """Return the on-demand hourly USD cost for an EC2 instance type, or None if unknown."""
    _load_cache()
    return _hourly_cost_by_instance_type.get(instance_type)


def get_token_costs(model_id):
    """Return (input_cost_per_million_tokens, output_cost_per_million_tokens) for a
    Bedrock model_id, or None if unknown."""
    _load_cache()
    return _token_cost_by_model_id.get(model_id)


def compute_token_cost(model_id, input_token_count, output_token_count):
    """Return the USD cost of a Bedrock call, or 0.0 if the model_id is unknown."""
    costs = get_token_costs(model_id)
    if costs is None:
        return 0.0
    input_cost_per_million, output_cost_per_million = costs
    return (
        input_token_count * input_cost_per_million
        + output_token_count * output_cost_per_million
    ) / 1_000_000


def compute_server_cost(instance_type, execution_time_seconds):
    """Return the USD cost of occupying an EC2 instance for execution_time_seconds,
    or 0.0 if the instance_type is unknown."""
    hourly_cost = get_hourly_cost(instance_type)
    if hourly_cost is None:
        return 0.0
    return hourly_cost * execution_time_seconds / 3600
