"""Tests for canyonos_core.OTLP_Exporter.otlp_utils, log_convert, and otel_exporter._process_signal."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

# The exporter modules import each other as top-level names when running from
# their own directory; add the package directory to resolve them in tests too.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ventis", "OTLP_Exporter")
))

# Stub out protobuf modules that are build artifacts and not present in source.
for _mod in ("local_controler_pb2", "local_controler_pb2_grpc"):
    if _mod not in sys.modules:
        _m = types.ModuleType(_mod)
        _m.JsonResponse = object
        _m.LocalControllerStub = object
        sys.modules[_mod] = _m

from canyonos_core.OTLP_Exporter import db, otlp_utils, log_convert  # noqa: E402
import otel_exporter  # noqa: E402


# ---------------------------------------------------------------------------
# otlp_utils
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# log_convert
# ---------------------------------------------------------------------------

def _make_log_row(
    future_id="00112233445566778899aabbccddeeff",
    session_id="ffeeddccbbaa99887766554433221100",
    logs=None,
    name="IntentAgent.parse",
):
    return {
        "future_id": future_id,
        "session_id": session_id,
        "name": name,
        "logs": logs,
    }


class WaitingRowToLogRecordsTests(unittest.TestCase):
    def _entry(self, severity_text="INFO", severity_number=9, body="hello",
                agent_id="a1", agent_name="IntentAgent", endpoint="1.2.3.4:50051",
                exception_type=None, exception_message=None, exception_stacktrace=None,
                timestamp=1.0):
        return {
            "Timestamp": timestamp,
            "ObservedTimestamp": timestamp,
            "SeverityNumber": severity_number,
            "SeverityText": severity_text,
            "Body": body,
            "TraceId": None,
            "SpanId": None,
            "Attributes": {
                "agent.id": agent_id,
                "agent.name": agent_name,
                "endpoint": endpoint,
                "logger.name": None,
                "exception.type": exception_type,
                "exception.message": exception_message,
                "exception.stacktrace": exception_stacktrace,
            },
            "Resource": {"service.name": agent_name},
        }

    def test_returns_empty_list_when_logs_is_none(self):
        row = _make_log_row(logs=None)
        self.assertEqual(log_convert.waiting_row_to_log_records(row), [])

    def test_returns_empty_list_when_logs_is_empty_json_array(self):
        row = _make_log_row(logs="[]")
        self.assertEqual(log_convert.waiting_row_to_log_records(row), [])

    def test_returns_empty_list_for_malformed_json(self):
        row = _make_log_row(logs="not-json")
        self.assertEqual(log_convert.waiting_row_to_log_records(row), [])

    # ReadableLogRecord wraps the inner LogRecord; access fields via .log_record.*
    def _lr(self, record):
        return record.log_record

    def test_converts_one_info_entry(self):
        entry = self._entry(severity_text="INFO", severity_number=9, body="Executing")
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        self.assertEqual(len(records), 1)
        self.assertEqual(self._lr(records[0]).body, "Executing")
        self.assertEqual(self._lr(records[0]).severity_text, "INFO")

    def test_remaps_warning_to_warn(self):
        entry = self._entry(severity_text="WARNING", severity_number=13)
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        self.assertEqual(self._lr(records[0]).severity_text, "WARN")

    def test_remaps_critical_to_fatal(self):
        entry = self._entry(severity_text="CRITICAL", severity_number=21)
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        self.assertEqual(self._lr(records[0]).severity_text, "FATAL")

    def test_error_entry_keeps_error_text(self):
        entry = self._entry(severity_text="ERROR", severity_number=17,
                             exception_type="ThrottlingException",
                             exception_message="Too many requests")
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        lr = self._lr(records[0])
        self.assertEqual(lr.severity_text, "ERROR")
        self.assertIn("exception.type", lr.attributes)
        self.assertEqual(lr.attributes["exception.type"], "ThrottlingException")

    def test_trace_and_span_ids_derived_from_row(self):
        future_id = "00112233445566778899aabbccddeeff"
        session_id = "ffeeddccbbaa99887766554433221100"
        entry = self._entry()
        row = _make_log_row(future_id=future_id, session_id=session_id,
                             logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        lr = self._lr(records[0])
        self.assertEqual(lr.trace_id, int(session_id, 16))
        self.assertEqual(lr.span_id, otlp_utils.span_id_from_future(future_id))

    def test_null_session_produces_zero_trace_id(self):
        # OTel SDK stores None trace_id as 0 (INVALID_SPAN_ID convention).
        entry = self._entry()
        row = _make_log_row(session_id=None, logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        self.assertIn(self._lr(records[0]).trace_id, (None, 0))

    def test_ventis_attributes_are_namespaced(self):
        entry = self._entry(agent_id="abc123", agent_name="PriceAgent",
                             endpoint="10.0.0.1:50051")
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        attrs = self._lr(records[0]).attributes
        self.assertEqual(attrs.get("ventis.agent.id"), "abc123")
        self.assertEqual(attrs.get("ventis.agent.name"), "PriceAgent")
        self.assertEqual(attrs.get("ventis.endpoint"), "10.0.0.1:50051")
        self.assertNotIn("agent.id", attrs)

    def test_null_attributes_are_excluded(self):
        entry = self._entry(exception_type=None, exception_message=None)
        row = _make_log_row(logs=json.dumps([entry]))
        records = log_convert.waiting_row_to_log_records(row)
        attrs = self._lr(records[0]).attributes
        self.assertNotIn("exception.type", attrs)
        self.assertNotIn("exception.message", attrs)

    def test_multiple_entries_produce_multiple_records(self):
        entries = [self._entry(body="first"), self._entry(body="second")]
        row = _make_log_row(logs=json.dumps(entries))
        records = log_convert.waiting_row_to_log_records(row)
        self.assertEqual(len(records), 2)
        self.assertEqual(self._lr(records[0]).body, "first")
        self.assertEqual(self._lr(records[1]).body, "second")


# ---------------------------------------------------------------------------
# otel_exporter._process_signal
# ---------------------------------------------------------------------------

class ProcessSignalTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        db.init_db(self.db_path)
        self._insert_row(
            future_id="aabbccdd" * 4,
            session_id="11223344" * 4,
            finished_at=2.0,
            sent=0,
            logs_sent=0,
            logs=json.dumps([{"SeverityText": "INFO", "Body": "ok",
                               "SeverityNumber": 9, "Attributes": {},
                               "Timestamp": 1.0, "ObservedTimestamp": 1.0}]),
        )

    def tearDown(self):
        os.unlink(self.db_path)

    def _insert_row(self, future_id, session_id, finished_at, sent, logs_sent, logs=None):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, finished_at, failed, "
                "name, sent, logs_sent, logs) VALUES (?, ?, ?, 0, 'A.b', ?, ?, ?)",
                (future_id, session_id, finished_at, sent, logs_sent, logs),
            )
            conn.commit()
        finally:
            conn.close()

    def test_delivers_item_to_every_processor_and_marks_sent(self):
        proc_a = MagicMock(name="a")
        proc_b = MagicMock(name="b")
        mark = MagicMock()

        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._process_signal(
                query="SELECT * FROM waiting WHERE finished_at IS NOT NULL AND sent = 0",
                convert_fn=lambda row: ["item"],
                processors=[("a", proc_a), ("b", proc_b)],
                emit_fn=lambda proc, item: proc.on_emit(item),
                mark_fn=mark,
                signal_name="test",
            )

        proc_a.on_emit.assert_called_once_with("item")
        proc_b.on_emit.assert_called_once_with("item")
        mark.assert_called_once()

    def test_row_not_marked_when_one_destination_fails(self):
        failing = MagicMock(name="failing")
        failing.on_emit.side_effect = RuntimeError("down")
        succeeding = MagicMock(name="succeeding")
        mark = MagicMock()

        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._process_signal(
                query="SELECT * FROM waiting WHERE finished_at IS NOT NULL AND sent = 0",
                convert_fn=lambda row: ["item"],
                processors=[("fail", failing), ("ok", succeeding)],
                emit_fn=lambda proc, item: proc.on_emit(item),
                mark_fn=mark,
                signal_name="test",
            )

        # Both processors attempted despite first failure
        failing.on_emit.assert_called_once()
        succeeding.on_emit.assert_called_once()
        # Row not marked because a destination failed
        mark.assert_not_called()

    def test_empty_convert_result_marks_row_done(self):
        mark = MagicMock()
        proc = MagicMock()

        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._process_signal(
                query="SELECT * FROM waiting WHERE finished_at IS NOT NULL AND sent = 0",
                convert_fn=lambda row: [],
                processors=[("p", proc)],
                emit_fn=lambda proc, item: proc.on_emit(item),
                mark_fn=mark,
                signal_name="test",
            )

        proc.on_emit.assert_not_called()
        mark.assert_called_once()

    def test_no_rows_returns_without_calling_processors(self):
        proc = MagicMock()
        mark = MagicMock()

        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._process_signal(
                query="SELECT * FROM waiting WHERE 1 = 0",  # never matches
                convert_fn=lambda row: ["item"],
                processors=[("p", proc)],
                emit_fn=lambda proc, item: proc.on_emit(item),
                mark_fn=mark,
                signal_name="test",
            )

        proc.on_emit.assert_not_called()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
