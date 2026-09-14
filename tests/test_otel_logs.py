"""Tests for the OTel *logs* pipeline: log_convert.log_row_to_log_records,
otel_writer.log_write_rows, otel_reader.log_mark_sent(_many), the log exporter build,
_log_send_pending, and the logs-specific empty-queue/prune/flush paths. (The trace side is
covered by test_otel_exporter_fanout.py; the metrics side by test_otel_metrics.py.)
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "canyonos_core", "otlp_exporter"))

from opentelemetry.sdk._logs.export import LogExportResult  # noqa: E402

from canyonos_core.controller.utils import otel_writer, schema  # noqa: E402
import otel_reader  # noqa: E402
import log_convert  # noqa: E402
import otel_exporter  # noqa: E402
from utils import otlp_utils  # noqa: E402

# otel_exporter imports the generated local-controller protobufs at module load only via
# trace_convert/global_controller paths it doesn't touch here; stub them like the fanout test.
for _name, _attr in (
    ("local_controler_pb2", "JsonResponse"),
    ("local_controler_pb2_grpc", "LocalControllerStub"),
):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        setattr(_mod, _attr, object)
        sys.modules[_name] = _mod


# ---------------------------------------------------------------------------
# log_convert.log_row_to_log_records
# ---------------------------------------------------------------------------

def _make_row(
    log_id="00112233445566778899aabbccddeeff:0",
    future_id="00112233445566778899aabbccddeeff",
    session_id="ffeeddccbbaa99887766554433221100",
    agent_id="a1",
    observed_at=1.0,
    severity_number=9,
    severity_text="INFO",
    body="hello",
    attributes=None,
):
    return {
        "log_id": log_id,
        "future_id": future_id,
        "session_id": session_id,
        "project_id": "proj",
        "agent_id": agent_id,
        "observed_at": observed_at,
        "severity_number": severity_number,
        "severity_text": severity_text,
        "body": body,
        "attributes": json.dumps(attributes) if attributes is not None else "{}",
    }


class LogRowToLogRecordsTests(unittest.TestCase):
    # ReadableLogRecord wraps the inner LogRecord; access fields via .log_record.*
    def _lr(self, record):
        return record.log_record

    def test_returns_empty_list_for_malformed_attributes_json(self):
        row = _make_row()
        row["attributes"] = "not-json"
        self.assertEqual(log_convert.log_row_to_log_records(row), [])

    def test_converts_one_info_row(self):
        row = _make_row(severity_text="INFO", severity_number=9, body="Executing")
        records = log_convert.log_row_to_log_records(row)
        self.assertEqual(len(records), 1)
        self.assertEqual(self._lr(records[0]).body, "Executing")
        self.assertEqual(self._lr(records[0]).severity_text, "INFO")

    def test_remaps_warning_to_warn(self):
        row = _make_row(severity_text="WARNING", severity_number=13)
        records = log_convert.log_row_to_log_records(row)
        self.assertEqual(self._lr(records[0]).severity_text, "WARN")

    def test_remaps_critical_to_fatal(self):
        row = _make_row(severity_text="CRITICAL", severity_number=21)
        records = log_convert.log_row_to_log_records(row)
        self.assertEqual(self._lr(records[0]).severity_text, "FATAL")

    def test_error_row_keeps_exception_attributes(self):
        row = _make_row(
            severity_text="ERROR",
            severity_number=17,
            attributes={
                "exception.type": "ThrottlingException",
                "exception.message": "Too many requests",
            },
        )
        records = log_convert.log_row_to_log_records(row)
        lr = self._lr(records[0])
        self.assertEqual(lr.severity_text, "ERROR")
        self.assertEqual(lr.attributes["exception.type"], "ThrottlingException")

    def test_trace_and_span_ids_derived_from_row(self):
        future_id = "00112233445566778899aabbccddeeff"
        session_id = "ffeeddccbbaa99887766554433221100"
        row = _make_row(future_id=future_id, session_id=session_id)
        records = log_convert.log_row_to_log_records(row)
        lr = self._lr(records[0])
        self.assertEqual(lr.trace_id, int(session_id, 16))
        self.assertEqual(lr.span_id, otlp_utils.span_id_from_future(future_id))

    def test_null_session_produces_zero_trace_id(self):
        # OTel SDK stores None trace_id as 0 (INVALID_SPAN_ID convention).
        row = _make_row(session_id=None)
        records = log_convert.log_row_to_log_records(row)
        self.assertIn(self._lr(records[0]).trace_id, (None, 0))

    def test_canyonos_attributes_are_namespaced(self):
        # Attributes are stored un-namespaced; log_convert namespaces them at export time.
        row = _make_row(
            attributes={
                "agent.id": "abc123",
                "agent.name": "PriceAgent",
                "endpoint": "10.0.0.1:50051",
            }
        )
        records = log_convert.log_row_to_log_records(row)
        attrs = self._lr(records[0]).attributes
        self.assertEqual(attrs.get("canyonos.agent.id"), "abc123")
        self.assertEqual(attrs.get("canyonos.agent.name"), "PriceAgent")
        self.assertEqual(attrs.get("canyonos.endpoint"), "10.0.0.1:50051")
        self.assertNotIn("agent.id", attrs)

    def test_null_attributes_are_excluded(self):
        row = _make_row(attributes={"exception.type": None, "exception.message": None})
        records = log_convert.log_row_to_log_records(row)
        attrs = self._lr(records[0]).attributes
        self.assertNotIn("exception.type", attrs)
        self.assertNotIn("exception.message", attrs)

    def test_timestamp_derived_from_observed_at(self):
        row = _make_row(observed_at=2.5)
        lr = self._lr(log_convert.log_row_to_log_records(row)[0])
        self.assertEqual(lr.timestamp, otlp_utils.to_epoch_nanos(2.5))
        self.assertEqual(lr.observed_timestamp, otlp_utils.to_epoch_nanos(2.5))


# ---------------------------------------------------------------------------
# otel_writer.log_write_rows
# ---------------------------------------------------------------------------

def _log_entry(severity_text="INFO", severity_number=9, body="hello", attrs=None,
               timestamp=1.0):
    return {
        "Timestamp": timestamp,
        "ObservedTimestamp": timestamp,
        "SeverityNumber": severity_number,
        "SeverityText": severity_text,
        "Body": body,
        "Attributes": attrs or {},
    }


def _future_row(future_id="f1", request_id="ffeeddccbbaa99887766554433221100", agent="a1", logs=None):
    return {"future_id": future_id, "request_id": request_id, "agent": agent, "logs": logs}


class WriteLogRowsTests(unittest.TestCase):
    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _rows(self):
        with sqlite3.connect(self.tmp) as conn:
            conn.row_factory = sqlite3.Row
            return {r["log_id"]: r for r in conn.execute("SELECT * FROM logs_waiting")}

    def test_logs_array_explodes_into_one_row_per_entry(self):
        row = _future_row(logs=json.dumps([_log_entry(body="first"), _log_entry(body="second")]))
        otel_writer.log_write_rows([row], db_path=self.tmp)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["f1:0"]["body"], "first")
        self.assertEqual(rows["f1:1"]["body"], "second")

    def test_log_id_is_future_id_colon_index(self):
        row = _future_row(future_id="futX", logs=json.dumps([_log_entry()]))
        otel_writer.log_write_rows([row], db_path=self.tmp)
        self.assertIn("futX:0", self._rows())

    def test_repolling_the_same_future_upserts_instead_of_duplicating(self):
        row = _future_row(logs=json.dumps([_log_entry(body="first")]))
        otel_writer.log_write_rows([row], db_path=self.tmp)
        otel_writer.log_write_rows([row], db_path=self.tmp)
        with sqlite3.connect(self.tmp) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM logs_waiting").fetchone()[0], 1
            )

    def test_sent_is_never_reset_by_a_rewrite(self):
        row = _future_row(logs=json.dumps([_log_entry()]))
        otel_writer.log_write_rows([row], db_path=self.tmp)
        otel_reader.log_mark_sent("f1:0", self.tmp)
        otel_writer.log_write_rows([row], db_path=self.tmp)
        self.assertEqual(self._rows()["f1:0"]["sent"], 1)

    def test_non_dict_entries_are_skipped_but_others_kept(self):
        entries = [_log_entry(body="good"), "not-a-dict"]
        row = _future_row(logs=json.dumps(entries))
        otel_writer.log_write_rows([row], db_path=self.tmp)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["f1:0"]["body"], "good")

    def test_row_missing_future_id_or_logs_is_skipped(self):
        otel_writer.log_write_rows(
            [_future_row(future_id=None, logs=json.dumps([_log_entry()]))], db_path=self.tmp
        )
        otel_writer.log_write_rows([_future_row(logs=None)], db_path=self.tmp)
        self.assertEqual(len(self._rows()), 0)

    def test_unparseable_logs_field_is_dropped(self):
        otel_writer.log_write_rows([_future_row(logs="not-json")], db_path=self.tmp)
        self.assertEqual(len(self._rows()), 0)


# ---------------------------------------------------------------------------
# otel_reader.log_mark_sent / log_mark_sent_many
# ---------------------------------------------------------------------------

class MarkSentTests(unittest.TestCase):
    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)
        otel_writer.log_write_rows(
            [_future_row(logs=json.dumps([_log_entry(body="a"), _log_entry(body="b")]))],
            db_path=self.tmp,
        )

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _sent(self, log_id):
        with sqlite3.connect(self.tmp) as conn:
            return conn.execute(
                "SELECT sent FROM logs_waiting WHERE log_id = ?", (log_id,)
            ).fetchone()[0]

    def test_log_mark_sent_marks_only_the_named_row(self):
        otel_reader.log_mark_sent("f1:0", self.tmp)
        self.assertEqual(self._sent("f1:0"), 1)
        self.assertEqual(self._sent("f1:1"), 0)

    def test_log_mark_sent_many_marks_every_listed_row(self):
        otel_reader.log_mark_sent_many(["f1:0", "f1:1"], self.tmp)
        self.assertEqual(self._sent("f1:0"), 1)
        self.assertEqual(self._sent("f1:1"), 1)

    def test_log_mark_sent_many_with_empty_list_is_a_noop(self):
        otel_reader.log_mark_sent_many([], self.tmp)
        self.assertEqual(self._sent("f1:0"), 0)


# ---------------------------------------------------------------------------
# otel_exporter._log_send_pending
# ---------------------------------------------------------------------------

class SendPendingLogsTests(unittest.TestCase):
    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)
        self._orig = otel_exporter._log_exporters

    def tearDown(self):
        otel_exporter._log_exporters = self._orig
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed(self, entries=None, future_id="f1"):
        otel_writer.log_write_rows(
            [_future_row(future_id=future_id, logs=json.dumps(entries or [_log_entry()]))],
            db_path=self.tmp,
        )

    def _sent(self, log_id="f1:0"):
        with sqlite3.connect(self.tmp) as conn:
            return conn.execute(
                "SELECT sent FROM logs_waiting WHERE log_id = ?", (log_id,)
            ).fetchone()[0]

    def test_success_exports_log_records_and_marks_sent(self):
        self._seed()
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("live", exporter)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        exporter.export.assert_called_once()
        self.assertEqual(len(exporter.export.call_args.args[0]), 1)
        self.assertEqual(self._sent(), 1)

    def test_failure_leaves_the_row_unsent(self):
        self._seed()
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.FAILURE
        otel_exporter._log_exporters = [("live", exporter)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        self.assertEqual(self._sent(), 0)

    def test_delivers_the_same_records_to_every_destination_and_marks_sent(self):
        self._seed()
        first = MagicMock(name="first")
        first.export.return_value = LogExportResult.SUCCESS
        second = MagicMock(name="second")
        second.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("first", first), ("second", second)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        first.export.assert_called_once()
        second.export.assert_called_once()
        self.assertEqual(first.export.call_args.args[0], second.export.call_args.args[0])
        self.assertEqual(self._sent(), 1)

    def test_tries_every_destination_and_leaves_row_unsent_when_one_fails(self):
        self._seed()
        failing = MagicMock(name="failing")
        failing.export.side_effect = RuntimeError("down")
        succeeding = MagicMock(name="succeeding")
        succeeding.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("failing", failing), ("succeeding", succeeding)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        failing.export.assert_called_once()
        succeeding.export.assert_called_once()
        self.assertEqual(self._sent(), 0)

    def test_unparseable_row_is_marked_done_without_exporting(self):
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO logs_waiting (log_id, future_id, session_id, observed_at, "
                "attributes, sent) VALUES ('bad:0', 'bad', 's1', 1.0, 'not-json', 0)"
            )
            conn.commit()
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("live", exporter)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        exporter.export.assert_not_called()
        self.assertEqual(self._sent("bad:0"), 1)

    def test_convert_failure_skips_the_row_without_marking_it_sent(self):
        self._seed(future_id="bad1")
        self._seed(future_id="aced")
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("live", exporter)]

        real_convert = log_convert.log_row_to_log_records

        def flaky(row):
            if row["future_id"] == "bad1":
                raise RuntimeError("boom")
            return real_convert(row)

        with patch.object(otel_exporter, "DB_PATH", self.tmp), patch.object(
            log_convert, "log_row_to_log_records", side_effect=flaky
        ):
            otel_exporter._log_send_pending()

        self.assertEqual(self._sent("bad1:0"), 0)
        self.assertEqual(self._sent("aced:0"), 1)

    def test_no_pending_rows_does_not_call_any_exporter(self):
        exporter = MagicMock(name="live")
        otel_exporter._log_exporters = [("live", exporter)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._log_send_pending()
        exporter.export.assert_not_called()

    def test_sent_count_is_logged(self):
        self._seed(entries=[_log_entry(), _log_entry()])
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("live", exporter)]
        with patch.object(otel_exporter, "DB_PATH", self.tmp), self.assertLogs(
            otel_exporter.logger, level="INFO"
        ) as captured:
            otel_exporter._log_send_pending()
        self.assertTrue(any("Exported 2 log record(s)" in m for m in captured.output))

    def test_raises_when_no_destinations_configured(self):
        otel_exporter._log_exporters = []
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            with self.assertRaises(RuntimeError):
                otel_exporter._log_send_pending()


class LogQueueStateTests(unittest.TestCase):
    """The logs empty-queue tracker is independent of the trace/metric ones."""

    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)
        self._orig = otel_exporter._log_exporters
        self._orig_trace = dict(otel_exporter._trace_empty_queue)
        self._orig_log = dict(otel_exporter._log_empty_queue)
        otel_exporter._trace_empty_queue = {"consecutive": 0, "warned_at": None}
        otel_exporter._log_empty_queue = {"consecutive": 0, "warned_at": None}
        exporter = MagicMock(name="live")
        exporter.export.return_value = LogExportResult.SUCCESS
        otel_exporter._log_exporters = [("live", exporter)]

    def tearDown(self):
        otel_exporter._log_exporters = self._orig
        otel_exporter._trace_empty_queue = self._orig_trace
        otel_exporter._log_empty_queue = self._orig_log
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_empty_logs_queue_warns_once_and_leaves_trace_tracker_untouched(self):
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            with self.assertLogs(otel_exporter.logger, level="WARNING") as captured:
                for _ in range(otel_exporter.EMPTY_QUEUE_WARNING_POLLS + 3):
                    otel_exporter._log_send_pending()
        warnings = [m for m in captured.output if "No log rows have ever appeared" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.tmp, warnings[0])
        self.assertEqual(otel_exporter._trace_empty_queue["consecutive"], 0)


class PruneExpiredLogsTests(unittest.TestCase):
    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed_log(self, log_id, observed_at, sent):
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO logs_waiting (log_id, future_id, session_id, observed_at, "
                "attributes, sent) VALUES (?, 'f', 's', ?, '{}', ?)",
                (log_id, observed_at, sent),
            )
            conn.commit()

    def _log_ids(self):
        with sqlite3.connect(self.tmp) as conn:
            return {r[0] for r in conn.execute("SELECT log_id FROM logs_waiting")}

    def test_prune_removes_aged_out_rows_regardless_of_sent(self):
        now = time.time()
        self._seed_log("old-sent", now - 10000, 1)
        self._seed_log("old-unsent", now - 10000, 0)
        self._seed_log("new-unsent", now - 10, 0)
        removed = otel_reader.prune_expired("logs_waiting", "observed_at", 600, self.tmp)
        self.assertEqual(removed, 2)
        self.assertEqual(self._log_ids(), {"new-unsent"})

    def test_prune_leaves_rows_with_a_null_timestamp(self):
        self._seed_log("unsent-null-ts", None, 0)
        self.assertEqual(otel_reader.prune_expired("logs_waiting", "observed_at", 0, self.tmp), 0)
        self.assertEqual(self._log_ids(), {"unsent-null-ts"})

    def test_prune_expired_rows_covers_the_logs_table(self):
        now = time.time()
        self._seed_log("old", now - otel_exporter.LOG_RETENTION_SECONDS - 60, 1)
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._prune_expired_rows()
        self.assertEqual(self._log_ids(), set())


class FlushModeLogsTests(unittest.TestCase):
    """With no OTel log destination configured, the exporter flushes logs_waiting each poll."""

    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        schema.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed(self):
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO logs_waiting (log_id, future_id, session_id, observed_at, "
                "attributes, sent) VALUES ('l-unsent', 'f', 's', 1.0, '{}', 0)"
            )
            conn.execute(
                "INSERT INTO logs_waiting (log_id, future_id, session_id, observed_at, "
                "attributes, sent) VALUES ('l-sent', 'f', 's', 1.0, '{}', 1)"
            )
            conn.commit()

    def _count(self):
        with sqlite3.connect(self.tmp) as conn:
            return conn.execute("SELECT COUNT(*) FROM logs_waiting").fetchone()[0]

    def test_flush_all_deletes_sent_and_unsent(self):
        self._seed()
        removed = otel_reader.flush_all("logs_waiting", self.tmp)
        self.assertEqual(removed, 2)
        self.assertEqual(self._count(), 0)

    def test_flush_pending_targets_only_the_logs_table(self):
        self._seed()
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO traces_waiting (future_id, session_id, started_at, finished_at, "
                "sent) VALUES ('f1','s1',1.0,2.0,0)"
            )
            conn.commit()
        with patch.object(otel_exporter, "DB_PATH", self.tmp):
            otel_exporter._flush_pending("logs_waiting", "log row(s)")
        self.assertEqual(self._count(), 0)
        with sqlite3.connect(self.tmp) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM traces_waiting").fetchone()[0], 1
            )


if __name__ == "__main__":
    unittest.main()
