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


class TraceIdFromSessionTests(unittest.TestCase):
    def test_converts_hex_session_id_to_int(self):
        session_id = "ffeeddccbbaa99887766554433221100"
        result = otlp_utils.trace_id_from_session(session_id)
        self.assertEqual(result, int(session_id, 16))

    def test_returns_none_for_none(self):
        self.assertIsNone(otlp_utils.trace_id_from_session(None))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(otlp_utils.trace_id_from_session(""))


class SpanIdFromFutureTests(unittest.TestCase):
    def test_truncates_to_64_bits(self):
        future_id = "00112233445566778899aabbccddeeff"
        result = otlp_utils.span_id_from_future(future_id)
        expected = int.from_bytes(bytes.fromhex(future_id)[:8], "big")
        self.assertEqual(result, expected)

    def test_returns_none_for_none(self):
        self.assertIsNone(otlp_utils.span_id_from_future(None))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(otlp_utils.span_id_from_future(""))

    def test_different_futures_with_shared_prefix_produce_same_span_id(self):
        # Documents the known lossy truncation: two future_ids that share their
        # first 8 bytes map to the same span_id.
        a = "aabbccdd11223344" + "0000000000000000"
        b = "aabbccdd11223344" + "ffffffffffffffff"
        self.assertEqual(otlp_utils.span_id_from_future(a), otlp_utils.span_id_from_future(b))


if __name__ == "__main__":
    unittest.main()
