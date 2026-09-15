"""Tests for canyonos_core.otlp_exporter.utils.otlp_utils."""

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "canyonos_core", "otlp_exporter"))

from utils import otlp_utils  # noqa: E402


class ToEpochNanosTests(unittest.TestCase):
    def test_converts_float_seconds_to_nanoseconds(self):
        self.assertEqual(otlp_utils.to_epoch_nanos(1.5), 1_500_000_000)

    def test_returns_none_for_none_input(self):
        self.assertIsNone(otlp_utils.to_epoch_nanos(None))

    def test_handles_integer_input(self):
        self.assertEqual(otlp_utils.to_epoch_nanos(1), 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
