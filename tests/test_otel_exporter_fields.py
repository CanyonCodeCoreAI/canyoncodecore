import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ventis.OTLP_Exporter import convert, db


class OTelExporterFieldTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_init_db_creates_waiting_table_with_full_schema(self):
        db.init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(waiting)").fetchall()
            }
        self.assertTrue({"name", "input", "output"}.issubset(columns))

    def test_fields_are_normalized_and_added_to_span(self):
        db.init_db(self.db_path)
        raw = {
            "future_id": "0011223344556677",  # 64-bit (16 hex chars), matches Future.id's format
            "request_id": "ffeeddccbbaa99887766554433221100",
            "service": "PriceAgent",
            "method": "get_history",
            "args": '{"ticker": "NVDA"}',
            "result": "plain text result",
            "created_at": "1.0",
            "finished_at": "2.0",
            "failed": "0",
        }

        with patch.object(db.pricing, "compute_token_cost", return_value=0.0):
            db.write_waiting_rows([raw], db_path=self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM waiting").fetchone()

        self.assertEqual(row["name"], "PriceAgent.get_history")
        self.assertEqual(row["input"], raw["args"])
        self.assertEqual(json.loads(row["output"]), raw["result"])

        span = convert.waiting_row_to_span(row)
        self.assertEqual(span.name, "PriceAgent.get_history")
        self.assertEqual(span.attributes["langfuse.observation.input"], raw["args"])
        self.assertEqual(
            span.attributes["langfuse.observation.output"], row["output"]
        )

    def test_error_message_is_wired_from_redis_error_field(self):
        db.init_db(self.db_path)
        raw = {
            "future_id": "1111222233334444",  # 64-bit (16 hex chars), matches Future.id's format
            "request_id": "88887777666655554444333322221111",
            "service": "AdvisorAgent",
            "method": "summarize",
            "args": '{"risk": "moderate"}',
            "result": "",
            "created_at": "1.0",
            "finished_at": "2.0",
            "failed": "1",
            "error": "agent exploded",
        }

        with patch.object(db.pricing, "compute_token_cost", return_value=0.0):
            db.write_waiting_rows([raw], db_path=self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM waiting").fetchone()

        self.assertEqual(row["error_message"], "agent exploded")

        span = convert.waiting_row_to_span(row)
        self.assertEqual(span.status.description, "agent exploded")
        self.assertEqual(
            span.events[0].attributes["exception.message"], "agent exploded"
        )


if __name__ == "__main__":
    unittest.main()
