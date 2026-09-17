import unittest
from unittest.mock import MagicMock, patch

import requests
import yaml

from canyonos_core.controller.utils import pricing_refresh


def _fixture_yaml_path(tmp_path):
    path = tmp_path / "llm_prices.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "gpt-4o-mini": {
                        "input_cost_per_million_tokens": 0.15,
                        "output_cost_per_million_tokens": 0.6,
                    },
                    "anthropic.claude-haiku-4-5-v1:0": {
                        "input_cost_per_million_tokens": 1.0,
                        "output_cost_per_million_tokens": 5.0,
                    },
                    "untracked-upstream-model": {
                        "input_cost_per_million_tokens": 2.0,
                        "output_cost_per_million_tokens": 4.0,
                    },
                },
                "instances": {"m5.large": 0.096},
            },
            sort_keys=False,
        )
    )
    return path


def _mock_response(json_body, status_ok=True):
    response = MagicMock()
    response.json.return_value = json_body
    if status_ok:
        response.raise_for_status.return_value = None
    else:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("bad status")
    return response


class PricingRefreshTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import pathlib

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.prices_path = _fixture_yaml_path(pathlib.Path(self._tmpdir.name))

    def _read(self):
        return yaml.safe_load(self.prices_path.read_text())

    @patch.object(pricing_refresh.requests, "get")
    def test_successful_refresh_updates_matched_models_only(self, mock_get):
        mock_get.return_value = _mock_response(
            {
                "gpt-4o-mini": {
                    "input_cost_per_token": 0.0002,
                    "output_cost_per_token": 0.0008,
                },
                "anthropic.claude-haiku-4-5-v1:0": {
                    "input_cost_per_token": 0.000002,
                    "output_cost_per_token": 0.00001,
                },
            }
        )

        result = pricing_refresh.refresh_llm_prices(prices_path=str(self.prices_path))

        self.assertTrue(result)
        data = self._read()
        self.assertAlmostEqual(
            data["models"]["gpt-4o-mini"]["input_cost_per_million_tokens"], 200.0
        )
        self.assertAlmostEqual(
            data["models"]["gpt-4o-mini"]["output_cost_per_million_tokens"], 800.0
        )
        self.assertAlmostEqual(
            data["models"]["anthropic.claude-haiku-4-5-v1:0"]["input_cost_per_million_tokens"],
            2.0,
        )
        # Not present upstream -- left exactly as it was.
        self.assertEqual(
            data["models"]["untracked-upstream-model"],
            {"input_cost_per_million_tokens": 2.0, "output_cost_per_million_tokens": 4.0},
        )
        # instances untouched
        self.assertEqual(data["instances"], {"m5.large": 0.096})

    @patch.object(pricing_refresh.requests, "get")
    def test_network_exception_leaves_file_untouched(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("no network")
        before = self.prices_path.read_text()

        result = pricing_refresh.refresh_llm_prices(prices_path=str(self.prices_path))

        self.assertFalse(result)
        self.assertEqual(self.prices_path.read_text(), before)

    @patch.object(pricing_refresh.requests, "get")
    def test_non_2xx_response_leaves_file_untouched(self, mock_get):
        mock_get.return_value = _mock_response({}, status_ok=False)
        before = self.prices_path.read_text()

        result = pricing_refresh.refresh_llm_prices(prices_path=str(self.prices_path))

        self.assertFalse(result)
        self.assertEqual(self.prices_path.read_text(), before)

    @patch.object(pricing_refresh.requests, "get")
    def test_missing_cost_fields_skips_that_model_but_others_still_refresh(self, mock_get):
        mock_get.return_value = _mock_response(
            {
                "gpt-4o-mini": {"input_cost_per_token": 0.0002},  # missing output cost
                "anthropic.claude-haiku-4-5-v1:0": {
                    "input_cost_per_token": 0.000002,
                    "output_cost_per_token": 0.00001,
                },
            }
        )

        result = pricing_refresh.refresh_llm_prices(prices_path=str(self.prices_path))

        self.assertTrue(result)
        data = self._read()
        # gpt-4o-mini left at its original values since the remote entry was incomplete.
        self.assertEqual(
            data["models"]["gpt-4o-mini"],
            {"input_cost_per_million_tokens": 0.15, "output_cost_per_million_tokens": 0.6},
        )
        self.assertAlmostEqual(
            data["models"]["anthropic.claude-haiku-4-5-v1:0"]["input_cost_per_million_tokens"],
            2.0,
        )

    @patch.object(pricing_refresh.requests, "get")
    def test_no_matches_leaves_file_untouched(self, mock_get):
        mock_get.return_value = _mock_response({"some-other-model": {"input_cost_per_token": 1}})
        before = self.prices_path.read_text()

        result = pricing_refresh.refresh_llm_prices(prices_path=str(self.prices_path))

        self.assertFalse(result)
        self.assertEqual(self.prices_path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
