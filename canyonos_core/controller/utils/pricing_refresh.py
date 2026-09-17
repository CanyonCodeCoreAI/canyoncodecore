"""Best-effort refresh of llm_prices.yaml's per-model token costs from an external catalog."""

import logging

import requests
import yaml

from canyonos_core.controller.utils import pricing

logger = logging.getLogger(__name__)

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
REQUEST_TIMEOUT_SECONDS = 5

_YAML_HEADER = (
    "# LLM token pricing (USD per 1M tokens) and EC2 instance hourly pricing (USD/hr).\n"
    "# Loaded in-memory by canyonos_core.controller.utils.pricing wherever it's imported.\n"
    "# The `models:` section is refreshed automatically at OTLP exporter startup from\n"
    f"# {LITELLM_PRICING_URL} -- edits to those entries may be overwritten.\n"
)


def refresh_llm_prices(prices_path=pricing.DEFAULT_PRICES_PATH, source_url=LITELLM_PRICING_URL):
    """Refresh the `models:` entries in prices_path from source_url's per-token costs.

    Only updates model ids already present in the file -- never adds or removes keys,
    and never touches `instances:`. Returns True if the file was rewritten, False on
    any failure (network, bad response, no matches) or if nothing changed -- this must
    never raise, since it runs unconditionally on every OTLP exporter process startup.
    """
    try:
        response = requests.get(source_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        remote_prices = response.json()

        with open(prices_path, "r") as f:
            local_prices = yaml.safe_load(f) or {}
        models = local_prices.get("models") or {}

        updated = []
        for model_id, costs in models.items():
            remote = remote_prices.get(model_id)
            if not isinstance(remote, dict):
                continue
            input_cost = remote.get("input_cost_per_token")
            output_cost = remote.get("output_cost_per_token")
            if not isinstance(input_cost, (int, float)) or not isinstance(
                output_cost, (int, float)
            ):
                continue
            costs["input_cost_per_million_tokens"] = input_cost * 1_000_000
            costs["output_cost_per_million_tokens"] = output_cost * 1_000_000
            updated.append(model_id)

        if not updated:
            logger.info("LLM price refresh: no matching models found in %s; leaving %s as-is.",
                        source_url, prices_path)
            return False

        with open(prices_path, "w") as f:
            f.write(_YAML_HEADER)
            yaml.safe_dump(local_prices, f, sort_keys=False)

        logger.info(
            "LLM price refresh: updated %d/%d model(s) in %s from %s.",
            len(updated), len(models), prices_path, source_url,
        )
        return True
    except Exception:
        logger.warning(
            "LLM price refresh from %s failed; keeping existing %s.",
            source_url, prices_path, exc_info=True,
        )
        return False
