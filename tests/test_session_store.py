import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text

import ventis.controller.utils.demo_obfuscation as demo_obfuscation
import ventis.controller.utils.session_store as session_store


def _stored_epoch(stored):
    """Unix epoch seconds held by a stored TIMESTAMPTZ, for asserting the shift."""
    return datetime.fromisoformat(str(stored)).replace(tzinfo=timezone.utc).timestamp()


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        session_store._engine = None
        # session_store no longer bootstraps the schema itself (that's expected
        # to already exist on the real database) -- tests create it directly.
        with session_store._get_engine("").begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE session (
                        session_id VARCHAR(255) PRIMARY KEY,
                        project_id UUID NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'running',
                        input JSONB,
                        output JSONB,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
            )

    def tearDown(self):
        session_store._engine = None
        os.unlink(self.db.name)

    def test_upsert_session_inserts_a_row_with_running_status(self):
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "running", 1000.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        self.assertEqual(row["project_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertEqual(
            _stored_epoch(row["created_at"]),
            1000.0 - demo_obfuscation.shift_for_session("req1"),
        )

    def test_upsert_session_transitions_status_in_place(self):
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "running", 1.0
        )
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "completed", 999.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        # created_at/project_id come from the first call and stay put; status
        # and updated_at reflect the second call. Both calls shift by the same
        # per-session offset, so the 998s the request took is preserved.
        shift = demo_obfuscation.shift_for_session("req1")
        self.assertEqual(_stored_epoch(row["created_at"]), 1.0 - shift)
        self.assertEqual(row["project_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(_stored_epoch(row["updated_at"]), 999.0 - shift)

    def test_upsert_session_input_and_output_round_trip(self):
        session_store.upsert_session(
            "",
            "11111111-1111-1111-1111-111111111111",
            "req1",
            "running",
            1.0,
            input_payload={"query": "abc"},
        )
        session_store.upsert_session(
            "",
            "11111111-1111-1111-1111-111111111111",
            "req1",
            "completed",
            999.0,
            output_payload={"result": 42},
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        # input from the first call stays put; output from the second call is
        # added without clobbering it.
        self.assertEqual(json.loads(row["input"]), {"query": "abc"})
        self.assertEqual(json.loads(row["output"]), {"result": 42})

    def test_upsert_session_first_call_can_be_any_status(self):
        # There's no separate insert-only path -- a first call with any status
        # (e.g. the workflow already failed before a "running" row existed)
        # just creates the row directly, no error.
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req-new", "failed", 1.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req-new'"))
                .mappings()
                .fetchone()
            )
        self.assertEqual(row["status"], "failed")

    def test_get_session_returns_status_and_output(self):
        session_store.upsert_session(
            "",
            "11111111-1111-1111-1111-111111111111",
            "req1",
            "completed",
            1.0,
            input_payload={"query": "abc"},
            output_payload={"result": 42},
        )
        row = session_store.get_session(
            "", "11111111-1111-1111-1111-111111111111", "req1"
        )
        self.assertEqual(row["status"], "completed")
        self.assertEqual(json.loads(row["output"]), {"result": 42})

    def test_get_session_returns_none_for_unknown_session(self):
        self.assertIsNone(
            session_store.get_session(
                "", "11111111-1111-1111-1111-111111111111", "nope"
            )
        )

    def test_get_session_is_scoped_to_the_project(self):
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "completed", 1.0
        )
        self.assertIsNone(
            session_store.get_session(
                "", "22222222-2222-2222-2222-222222222222", "req1"
            )
        )


if __name__ == "__main__":
    unittest.main()
