"""Lookups over LLM/instance pricing published to Redis (from llm_prices.yaml) at controller init."""

import json
import os
import re
import time
import weakref

import yaml

TOKEN_PRICING_KEY = "llm_pricing:tokens"
SERVER_PRICING_KEY = "llm_pricing:instances"

DEFAULT_PRICES_PATH = os.path.join(os.path.dirname(__file__), "llm_prices.yaml")

# Each hash is small (tens to low hundreds of fields) and changes only when an
# operator edits llm_prices.yaml and restarts the global controller, so it's
# cheap to pull the whole hash and cache it in-process instead of round-tripping
# Redis on every cost lookup. Keyed by redis_client (weakly, so a client's cache
# dies with it) rather than a plain module dict, so distinct clients -- e.g. two
# different hosts' node Redis in tests or in a multi-node deploy -- never share
# cached data with each other.
_CACHE_TTL_SECONDS = 30
_hash_cache = weakref.WeakKeyDictionary()


def _cached_hash(redis_client, key):
    client_cache = _hash_cache.setdefault(redis_client, {})
    cached = client_cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    data = redis_client.hgetall(key)
    client_cache[key] = (data, now)
    return data


def load_pricing_data(prices_path=DEFAULT_PRICES_PATH):
    """Read llm_prices.yaml and return the (token, instance) Redis hash mappings it publishes."""
    with open(prices_path, "r") as f:
        prices = yaml.safe_load(f) or {}

    token_mapping = {
        model_id: json.dumps(
            [costs["input_cost_per_million_tokens"], costs["output_cost_per_million_tokens"]]
        )
        for model_id, costs in (prices.get("models") or {}).items()
    }
    instance_mapping = {
        instance_type: str(hourly_cost)
        for instance_type, hourly_cost in (prices.get("instances") or {}).items()
    }
    return token_mapping, instance_mapping


def _candidate_model_ids(model_id):
    """Yield the pricing keys a model id could match, most specific first."""
    yield model_id
    if not model_id or not model_id.startswith("claude-"):
        return
    # Direct-API Anthropic ids carry no vendor prefix or version suffix, and the
    # table is inconsistent about the date segment, so try both spellings.
    yield f"anthropic.{model_id}-v1:0"
    undated = re.sub(r"-\d{8}$", "", model_id)
    if undated != model_id:
        yield f"anthropic.{undated}-v1:0"


def compute_token_cost(redis_client, model_id, input_token_count, output_token_count):
    """Return the USD cost of an LLM call, or 0.0 if the model_id is unknown."""
    if not model_id:
        return 0.0
    token_prices = _cached_hash(redis_client, TOKEN_PRICING_KEY)
    costs_json = None
    for candidate in _candidate_model_ids(model_id):
        costs_json = token_prices.get(candidate)
        if costs_json is not None:
            break
    if costs_json is None:
        return 0.0
    input_cost_per_million, output_cost_per_million = json.loads(costs_json)
    return (
        input_token_count * input_cost_per_million
        + output_token_count * output_cost_per_million
    ) / 1_000_000


def compute_server_cost(redis_client, instance_type, execution_time_seconds):
    """Return the USD cost of occupying an EC2 instance for execution_time_seconds,
    or 0.0 if the instance_type is unknown."""
    if not instance_type:
        return 0.0
    instance_prices = _cached_hash(redis_client, SERVER_PRICING_KEY)
    hourly_cost = instance_prices.get(instance_type)
    if hourly_cost is None:
        return 0.0
    return float(hourly_cost) * execution_time_seconds / 3600
