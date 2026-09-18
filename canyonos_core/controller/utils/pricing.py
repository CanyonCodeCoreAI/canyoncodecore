"""Lazily-cached lookups over the static llm_token_costs.db reference data."""

import os
import re

from sqlalchemy import create_engine, text

_PRICING_DB_PATH = os.path.join(os.path.dirname(__file__), "llm_token_costs.db")

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
                "output_cost_per_million_tokens FROM llm_token_costs"
            )
        ).fetchall()
    engine.dispose()

    _hourly_cost_by_instance_type = {row[0]: row[1] for row in instance_rows}
    _token_cost_by_model_id = {row[0]: (row[1], row[2]) for row in model_rows}


def _candidate_model_ids(model_id):
    """Yield the pricing keys a model id could match, most specific first."""
    if not model_id:
        return
    yield model_id
    undated = re.sub(r"-\d{4}-?\d{2}-?\d{2}$", "", model_id)
    if undated != model_id:
        yield undated
    if not model_id.startswith("claude-"):
        return
    # Direct-API Anthropic ids carry no vendor prefix or version suffix, and the
    # table is inconsistent about the date segment, so try both spellings.
    yield f"anthropic.{model_id}-v1:0"
    if undated != model_id:
        yield f"anthropic.{undated}-v1:0"


def compute_token_cost(model_id, input_token_count, output_token_count):
    """Return the USD cost of an LLM call, or 0.0 if the model_id is unknown."""
    _load_cache()
    costs = None
    for candidate in _candidate_model_ids(model_id):
        costs = _token_cost_by_model_id.get(candidate)
        if costs is not None:
            break
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
    _load_cache()
    hourly_cost = _hourly_cost_by_instance_type.get(instance_type)
    if hourly_cost is None:
        return 0.0
    return hourly_cost * execution_time_seconds / 3600
