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

    def test_init_db_migrates_existing_waiting_table(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE waiting (future_id TEXT PRIMARY KEY, session_id TEXT NOT NULL)"
            )

        db.init_db(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(waiting)").fetchall()
            }
        self.assertTrue({"name", "input", "output"}.issubset(columns))

    def test_fields_are_normalized_and_added_to_span(self):
        db.init_db(self.db_path)
        raw = {
            "future_id": "00112233445566778899aabbccddeeff",
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

    def test_split_hash_result_is_loaded_for_finished_rows(self):
        class SplitHashRedis:
            def hget(self, key, field):
                self.request = (key, field)
                return '{"recommendation": "hold"}'

            def get(self, key):
                return None

        db.init_db(self.db_path)
        redis = SplitHashRedis()
        raw = {
            "future_id": "11112222333344445555666677778888",
            "request_id": "88887777666655554444333322221111",
            "service": "AdvisorAgent",
            "method": "summarize",
            "args": '{"risk": "moderate"}',
            "result": "",
            "created_at": "1.0",
            "finished_at": "2.0",
            "failed": "0",
        }

        with patch.object(db.pricing, "compute_token_cost", return_value=0.0):
            db.write_waiting_rows([raw], redis_client=redis, db_path=self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            output = conn.execute("SELECT output FROM waiting").fetchone()[0]
        self.assertEqual(redis.request, (f"future:{raw['future_id']}", "result"))
        self.assertEqual(json.loads(output), {"recommendation": "hold"})


if __name__ == "__main__":
    unittest.main()
