"""Covers the telemetry feed in OTLP_Exporter/telemetry.py: ``pull_telemetry`` (scan
future:* hashes from a node's Redis) and ``send_telemetry`` (pull + queue into the
``traces_waiting`` table). Replaces the old test_telemetry_logging.py now that the legacy
Postgres writers are gone.
"""

import fnmatch
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.OTLP_Exporter import db
from canyonos_core.OTLP_Exporter.telemetry import pull_telemetry, send_telemetry


class _FakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes

    def scan_keys(self, pattern):
        return [k for k in self.hashes if fnmatch.fnmatch(k, pattern)]

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def get(self, name):
        return self.hashes.get(name)


class PullTelemetryTests(unittest.TestCase):
    def test_skips_children_and_consumers_and_stamps_future_id(self):
        redis = _FakeRedis(
            {
                "future:abc": {"id": "abc", "request_id": "req1"},
                "future:noid": {"request_id": "req2"},  # future_id falls back to key
                "future:abc:children": {"y": "1"},  # skipped
                "future:abc:consumers": {"x": "1"},  # skipped
            }
        )
        rows = pull_telemetry(redis)
        by_id = {r["future_id"]: r for r in rows}
        self.assertEqual(set(by_id), {"abc", "noid"})
        self.assertEqual(by_id["noid"]["future_id"], "noid")


class SendTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _waiting(self):
        with sqlite3.connect(self.tmp) as conn:
            return {
                r[0]: r[1]
                for r in conn.execute("SELECT future_id, session_id FROM traces_waiting")
            }

    def test_queues_rows_including_in_flight_futures(self):
        redis = _FakeRedis(
            {
                "future:done": {
                    "id": "done",
                    "request_id": "req1",
                    "agent": "a1",
                    "created_at": "1.0",
                    "finished_at": "9.0",
                },
                # In-flight (no finished_at) -- kept in `traces_waiting`, unlike the old
                # runtime_information writer that skipped unfinished rows.
                "future:live": {"id": "live", "request_id": "req2", "created_at": "5.0"},
            }
        )
        send_telemetry(redis, project_id="proj-1", db_path=self.tmp)
        self.assertEqual(self._waiting(), {"done": "req1", "live": "req2"})

    def test_row_missing_request_id_is_dropped(self):
        redis = _FakeRedis({"future:orphan": {"id": "orphan"}})
        send_telemetry(redis, project_id="proj-1", db_path=self.tmp)
        self.assertEqual(self._waiting(), {})


if __name__ == "__main__":
    unittest.main()
