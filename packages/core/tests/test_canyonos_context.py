import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import canyonos_core.controller.canyonos_context as canyonos_context


class CanyonosContextTests(unittest.TestCase):
    def setUp(self):
        canyonos_context._local = canyonos_context.threading.local()

    def tearDown(self):
        canyonos_context._local = canyonos_context.threading.local()

    def test_request_id_defaults_to_empty_string(self):
        self.assertEqual(canyonos_context.get_request_id(), "")

    def test_request_id_round_trips(self):
        canyonos_context.set_request_id("req-123")
        self.assertEqual(canyonos_context.get_request_id(), "req-123")

    def test_current_future_id_defaults_to_empty_string(self):
        self.assertEqual(canyonos_context.get_current_future_id(), "")

    def test_current_future_id_round_trips(self):
        canyonos_context.set_current_future_id("future-abc")
        self.assertEqual(canyonos_context.get_current_future_id(), "future-abc")

    def test_request_id_and_future_id_are_independent(self):
        canyonos_context.set_request_id("req-123")
        canyonos_context.set_current_future_id("future-abc")
        self.assertEqual(canyonos_context.get_request_id(), "req-123")
        self.assertEqual(canyonos_context.get_current_future_id(), "future-abc")

    def test_current_metrics_key_defaults_to_empty_string(self):
        self.assertEqual(canyonos_context.get_current_metrics_key(), "")

    def test_current_metrics_key_round_trips(self):
        canyonos_context.set_current_metrics_key("controller:localhost:50051:metrics")
        self.assertEqual(
            canyonos_context.get_current_metrics_key(),
            "controller:localhost:50051:metrics",
        )


if __name__ == "__main__":
    unittest.main()
