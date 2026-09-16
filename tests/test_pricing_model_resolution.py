import json
import time
import unittest
from unittest.mock import patch

from canyonos_core.controller.utils import pricing


class _FakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes
        self.hgetall_calls = 0

    def hgetall(self, name):
        self.hgetall_calls += 1
        return dict(self.hashes.get(name, {}))


def _redis_with_real_pricing():
    token_mapping, instance_mapping = pricing.load_pricing_data()
    return _FakeRedis(
        {
            pricing.TOKEN_PRICING_KEY: token_mapping,
            pricing.SERVER_PRICING_KEY: instance_mapping,
        }
    )


class PricingModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.redis = _redis_with_real_pricing()

    def test_bedrock_id_prices_unchanged(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost(
                self.redis, "anthropic.claude-haiku-4-5-v1:0", 1_000_000, 0
            ),
            1.0,
        )

    def test_undated_direct_anthropic_id_resolves(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost(
                self.redis, "claude-haiku-4-5", 1_000_000, 1_000_000
            ),
            6.0,
        )

    def test_dated_direct_anthropic_id_resolves_to_undated_row(self):
        self.assertEqual(
            pricing.compute_token_cost(
                self.redis, "claude-haiku-4-5-20251001", 1_000, 500
            ),
            pricing.compute_token_cost(
                self.redis, "anthropic.claude-haiku-4-5-v1:0", 1_000, 500
            ),
        )

    def test_dated_direct_anthropic_id_prefers_its_own_dated_row(self):
        self.assertEqual(
            pricing.compute_token_cost(
                self.redis, "claude-3-5-haiku-20241022", 1_000_000, 0
            ),
            pricing.compute_token_cost(
                self.redis, "anthropic.claude-3-5-haiku-20241022-v1:0", 1_000_000, 0
            ),
        )

    def test_openai_id_prices_non_zero(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost(self.redis, "gpt-4o-mini", 1_000_000, 1_000_000),
            0.75,
        )

    def test_unknown_model_still_returns_zero(self):
        self.assertEqual(
            pricing.compute_token_cost(self.redis, "no-such-model", 10_000, 10_000), 0.0
        )

    def test_missing_model_id_returns_zero(self):
        self.assertEqual(pricing.compute_token_cost(self.redis, None, 10_000, 10_000), 0.0)

    def test_instance_hourly_cost_resolves(self):
        self.assertAlmostEqual(
            pricing.compute_server_cost(self.redis, "m5.large", 3600), 0.096
        )

    def test_unknown_instance_type_returns_zero(self):
        self.assertEqual(
            pricing.compute_server_cost(self.redis, "no-such-instance", 3600), 0.0
        )


class PricingCacheTests(unittest.TestCase):
    def _redis(self, model_prices, instance_prices=None):
        return _FakeRedis(
            {
                pricing.TOKEN_PRICING_KEY: {
                    model_id: json.dumps(costs) for model_id, costs in model_prices.items()
                },
                pricing.SERVER_PRICING_KEY: instance_prices or {},
            }
        )

    def test_repeated_lookups_reuse_the_cached_hash(self):
        redis = self._redis({"gpt-4o-mini": [0.15, 0.6]})
        for _ in range(5):
            pricing.compute_token_cost(redis, "gpt-4o-mini", 1_000_000, 1_000_000)
        self.assertEqual(redis.hgetall_calls, 1)

    def test_cache_expires_after_ttl(self):
        redis = self._redis({"gpt-4o-mini": [0.15, 0.6]})
        pricing.compute_token_cost(redis, "gpt-4o-mini", 1, 1)
        future = time.monotonic() + pricing._CACHE_TTL_SECONDS + 1
        with patch.object(pricing.time, "monotonic", return_value=future):
            pricing.compute_token_cost(redis, "gpt-4o-mini", 1, 1)
        self.assertEqual(redis.hgetall_calls, 2)

    def test_cache_does_not_leak_between_redis_clients(self):
        redis_a = self._redis({"gpt-4o-mini": [0.15, 0.6]})
        redis_b = self._redis({"gpt-4o-mini": [99.0, 99.0]})
        cost_a = pricing.compute_token_cost(redis_a, "gpt-4o-mini", 1_000_000, 1_000_000)
        cost_b = pricing.compute_token_cost(redis_b, "gpt-4o-mini", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost_a, 0.75)
        self.assertAlmostEqual(cost_b, 198.0)


if __name__ == "__main__":
    unittest.main()
