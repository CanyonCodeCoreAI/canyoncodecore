import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text

import ventis.controller.utils.session_store as session_store


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        session_store._engine = None

    def tearDown(self):
        session_store._engine = None
        os.unlink(self.db.name)

    def test_upsert_session_inserts_a_row_with_working_status(self):
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "working", 1000.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        self.assertEqual(row["project_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertIn("1970-01-01 00:16:40", row["created_at"])

    def test_upsert_session_transitions_status_in_place(self):
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "working", 1.0
        )
        session_store.upsert_session(
            "", "11111111-1111-1111-1111-111111111111", "req1", "success", 999.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        # created_at/project_id come from the first call and stay put; status
        # and updated_at reflect the second call.
        self.assertIn("1970-01-01 00:00:01", row["created_at"])
        self.assertEqual(row["project_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "success")
        self.assertIn("1970-01-01 00:16:39", row["updated_at"])

    def test_upsert_session_first_call_can_be_any_status(self):
        # There's no separate insert-only path -- a first call with any status
        # (e.g. the workflow already failed before a "working" row existed)
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


if __name__ == "__main__":
    unittest.main()
