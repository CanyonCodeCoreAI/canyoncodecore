import unittest

from canyonos_core.controller.utils import pricing


class PricingModelResolutionTests(unittest.TestCase):
    def test_bedrock_id_prices_unchanged(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost("anthropic.claude-haiku-4-5-v1:0", 1_000_000, 0),
            1.0,
        )

    def test_undated_direct_anthropic_id_resolves(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost("claude-haiku-4-5", 1_000_000, 1_000_000),
            6.0,
        )

    def test_dated_direct_anthropic_id_resolves_to_undated_row(self):
        self.assertEqual(
            pricing.compute_token_cost("claude-haiku-4-5-20251001", 1_000, 500),
            pricing.compute_token_cost("anthropic.claude-haiku-4-5-v1:0", 1_000, 500),
        )

    def test_dated_direct_anthropic_id_prefers_its_own_dated_row(self):
        self.assertEqual(
            pricing.compute_token_cost("claude-3-5-haiku-20241022", 1_000_000, 0),
            pricing.compute_token_cost(
                "anthropic.claude-3-5-haiku-20241022-v1:0", 1_000_000, 0
            ),
        )

    def test_openai_id_prices_non_zero(self):
        self.assertAlmostEqual(
            pricing.compute_token_cost("gpt-4o-mini", 1_000_000, 1_000_000), 0.75
        )

    def test_unknown_model_still_returns_zero(self):
        self.assertEqual(
            pricing.compute_token_cost("no-such-model", 10_000, 10_000), 0.0
        )


if __name__ == "__main__":
    unittest.main()
