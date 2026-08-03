import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text

import ventis.controller.utils.demo_obfuscation as demo_obfuscation
import ventis.controller.utils.session_store as session_store
import ventis.controller.utils.sqlalchemy as sqlmod


def _stored_epoch(stored):
    """Unix epoch seconds held by a stored TIMESTAMPTZ."""
    return datetime.fromisoformat(str(stored)).replace(tzinfo=timezone.utc).timestamp()


class ShiftForSessionTests(unittest.TestCase):
    def test_is_deterministic_for_the_same_session(self):
        self.assertEqual(
            demo_obfuscation.shift_for_session("req1"),
            demo_obfuscation.shift_for_session("req1"),
        )

    def test_differs_between_sessions(self):
        self.assertNotEqual(
            demo_obfuscation.shift_for_session("req1"),
            demo_obfuscation.shift_for_session("req2"),
        )

    def test_stays_inside_the_configured_window(self):
        for session_id in ("req1", "req2", "", 0, "a" * 200):
            shift = demo_obfuscation.shift_for_session(session_id)
            self.assertGreaterEqual(shift, 0)
            self.assertLess(shift, demo_obfuscation.RANDOM_SHIFT_MAX_SECONDS)

    def test_accepts_non_string_session_ids(self):
        # request ids arrive as str everywhere today, but project_id defaults to
        # the int 0 in global_controller, so don't blow up on non-str input.
        self.assertEqual(
            demo_obfuscation.shift_for_session(1234),
            demo_obfuscation.shift_for_session("1234"),
        )


class ToTimestamptzTests(unittest.TestCase):
    def test_subtracts_the_shift_and_returns_utc(self):
        result = demo_obfuscation.to_timestamptz(1000.0, 400)
        self.assertEqual(result.timestamp(), 600.0)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_accepts_epoch_seconds_as_text(self):
        # Redis hands metrics back as strings.
        self.assertEqual(
            demo_obfuscation.to_timestamptz("1000.0", 400),
            demo_obfuscation.to_timestamptz(1000.0, 400),
        )

    def test_preserves_intervals_within_a_session(self):
        shift = demo_obfuscation.shift_for_session("req1")
        start = demo_obfuscation.to_timestamptz(1_700_000_000.0, shift)
        end = demo_obfuscation.to_timestamptz(1_700_000_012.5, shift)
        self.assertEqual((end - start).total_seconds(), 12.5)


class SharedShiftAcrossWritersTests(unittest.TestCase):
    """The reason the shift lives in its own module: the `session` row and the
    runtime_information rows for that session's futures are written by two
    different processes, and they have to land on one timeline."""

    PROJECT_ID = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        # Both writers keep their own module-global engine; point both at the
        # same file so one assertion can compare what they wrote.
        session_store._engine = None
        sqlmod._engine = None
        sqlmod._project_id = self.PROJECT_ID
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
        sqlmod._engine = None
        sqlmod._project_id = None
        os.environ.pop("VENTIS_DATABASE_URL", None)
        os.unlink(self.db.name)

    def test_session_and_runtime_rows_share_one_timeline(self):
        dispatched_at = 1_700_000_000.0
        future_started_at = dispatched_at + 1.0
        future_finished_at = dispatched_at + 4.0
        completed_at = dispatched_at + 5.0

        session_store.upsert_session(
            "", self.PROJECT_ID, "req1", "running", dispatched_at
        )
        sqlmod.send_runtime_information(
            [
                {
                    "future_id": "fut1",
                    "request_id": "req1",
                    "created_at": str(future_started_at),
                    "finished_at": str(future_finished_at),
                }
            ]
        )
        session_store.upsert_session(
            "", self.PROJECT_ID, "req1", "completed", completed_at
        )

        with session_store._get_engine("").connect() as conn:
            session_row = (
                conn.execute(
                    text("SELECT * FROM session WHERE session_id='req1'")
                )
                .mappings()
                .fetchone()
            )
            runtime_row = (
                conn.execute(
                    text("SELECT * FROM runtime_information WHERE future_id='fut1'")
                )
                .mappings()
                .fetchone()
            )

        shift = demo_obfuscation.shift_for_session("req1")
        self.assertEqual(
            _stored_epoch(session_row["created_at"]), dispatched_at - shift
        )
        self.assertEqual(
            _stored_epoch(runtime_row["started_at"]), future_started_at - shift
        )
        self.assertEqual(
            _stored_epoch(runtime_row["finished_at"]), future_finished_at - shift
        )
        self.assertEqual(
            _stored_epoch(session_row["updated_at"]), completed_at - shift
        )

        # The whole point: the future ran inside its own session's window, and the
        # real durations survived the shift.
        self.assertLessEqual(
            _stored_epoch(session_row["created_at"]),
            _stored_epoch(runtime_row["started_at"]),
        )
        self.assertGreaterEqual(
            _stored_epoch(session_row["updated_at"]),
            _stored_epoch(runtime_row["finished_at"]),
        )
        self.assertEqual(
            _stored_epoch(session_row["updated_at"])
            - _stored_epoch(session_row["created_at"]),
            completed_at - dispatched_at,
        )

    def test_shift_is_applied_at_all(self):
        # Guards against the shift silently regressing to a no-op, which would
        # publish real wall-clock times.
        session_store.upsert_session(
            "", self.PROJECT_ID, "req1", "running", 1_700_000_000.0
        )
        with session_store._get_engine("").connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM session WHERE session_id='req1'"))
                .mappings()
                .fetchone()
            )
        self.assertNotEqual(_stored_epoch(row["created_at"]), 1_700_000_000.0)


if __name__ == "__main__":
    unittest.main()
