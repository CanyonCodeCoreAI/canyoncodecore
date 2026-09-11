"""Focused tests for the CanyonOS OTel exporter fan-out configuration."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# ``otel_exporter.py`` is also executed as a script from its own directory and
# therefore imports ``convert`` and ``db`` as top-level modules.
sys.path.insert(0, os.path.join(ROOT, "canyonos_core", "OTLP_Exporter"))

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace.export import SpanExportResult  # noqa: E402

import convert  # noqa: E402
import db  # noqa: E402
import otel_exporter  # noqa: E402


# The generated local-controller protobuf modules are build artifacts and are
# not present in a source checkout.  The static config helper does not use them,
# so provide the tiny import-time surface needed to test it in isolation.
if "local_controler_pb2" not in sys.modules:
    local_pb2 = types.ModuleType("local_controler_pb2")
    local_pb2.JsonResponse = object
    sys.modules["local_controler_pb2"] = local_pb2
if "local_controler_pb2_grpc" not in sys.modules:
    local_pb2_grpc = types.ModuleType("local_controler_pb2_grpc")
    local_pb2_grpc.LocalControllerStub = object
    sys.modules["local_controler_pb2_grpc"] = local_pb2_grpc


class OTelExporterFanoutTests(unittest.TestCase):
    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.db_file.name
        self.db_file.close()
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    @staticmethod
    def _destination_config():
        return [
            {
                "name": "railway",
                "protocol": "grpc",
                "endpoint": "receiver.example:4317",
                "headers": {"x-api-key": "railway-key"},
                "insecure": True,
                "timeout": 3.5,
            },
            {
                "name": "langfuse",
                "protocol": "http/protobuf",
                "endpoint": "https://langfuse.example/api/public/otel",
                "headers": {"authorization": "Basic secret"},
                "timeout": 7,
            },
        ]

    def test_build_exporters_constructs_mixed_exporters_with_explicit_args(self):
        grpc_exporter = object()
        http_exporter = object()
        destinations = self._destination_config()

        with (
            patch.object(
                otel_exporter,
                "GrpcOTLPSpanExporter",
                return_value=grpc_exporter,
            ) as grpc_constructor,
            patch.object(
                otel_exporter,
                "HttpOTLPSpanExporter",
                return_value=http_exporter,
            ) as http_constructor,
        ):
            exporters = otel_exporter._build_exporters(json.dumps(destinations))

        self.assertEqual(
            exporters, [("railway", grpc_exporter), ("langfuse", http_exporter)]
        )
        grpc_constructor.assert_called_once_with(
            endpoint="receiver.example:4317",
            headers={"x-api-key": "railway-key"},
            timeout=3.5,
            insecure=True,
        )
        http_constructor.assert_called_once_with(
            endpoint="https://langfuse.example/api/public/otel",
            headers={"authorization": "Basic secret"},
            timeout=7,
            session=unittest.mock.ANY,
        )

    def test_build_exporters_raises_when_destinations_raw_is_none(self):
        with self.assertRaisesRegex(RuntimeError, "otel.destinations is required"):
            otel_exporter._build_exporters(None)

    def test_configured_destinations_rejects_malformed_empty_and_duplicate_values(self):
        invalid_values = [
            "not-json",
            json.dumps([]),
            json.dumps(
                [
                    {
                        "name": "same",
                        "protocol": "grpc",
                        "endpoint": "one:4317",
                    },
                    {
                        "name": "same",
                        "protocol": "http/protobuf",
                        "endpoint": "https://two",
                    },
                ]
            ),
        ]
        for raw in invalid_values:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    otel_exporter._configured_destinations(raw)

    def test_controller_expands_env_in_destinations(self):
        # NOTE: the pre-existing Basic-auth-header-injection expectation this test
        # once carried was already unimplemented/failing before the Redis-backed
        # reload change (CANYONOS_OTEL_DESTINATIONS -> otel:destinations); out of
        # scope here, so this only covers ${ENV_VAR} expansion, which does work.
        from canyonos_core.controller.global_controller import GlobalController

        with patch.dict(
            os.environ,
            {"LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com"},
            clear=True,
        ):
            destinations = GlobalController._otel_destinations(
                {
                    "destinations": [
                        {
                            "name": "langfuse",
                            "protocol": "http/protobuf",
                            "endpoint": "${LANGFUSE_BASE_URL}/api/public/otel/v1/traces",
                        }
                    ]
                }
            )

        self.assertEqual(
            destinations[0]["endpoint"],
            "https://us.cloud.langfuse.com/api/public/otel/v1/traces",
        )

    def test_controller_destinations_is_none_when_otel_not_configured(self):
        from canyonos_core.controller.global_controller import GlobalController

        self.assertIsNone(GlobalController._otel_destinations({}))

    def _insert_pending_row(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO waiting (
                    future_id, session_id, started_at, finished_at, failed,
                    name, input, output, sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    "0011223344556677",
                    "ffeeddccbbaa99887766554433221100",
                    1.0,
                    2.0,
                    0,
                    "PriceAgent.get_history",
                    '{"ticker":"NVDA"}',
                    '{"price":100}',
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _assert_row_unsent(self):
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT sent FROM waiting").fetchone()[0], 0)
        finally:
            conn.close()

    def test_send_pending_delivers_the_same_spans_to_every_destination(self):
        self._insert_pending_row()
        first = MagicMock(name="first")
        first.export.return_value = SpanExportResult.SUCCESS
        second = MagicMock(name="second")
        second.export.return_value = SpanExportResult.SUCCESS
        with (
            patch.object(otel_exporter.db, "DB_PATH", self.db_path),
            patch.object(otel_exporter.db, "mark_sent_many") as mark_sent_many,
        ):
            otel_exporter._exporters = [("railway", first), ("langfuse", second)]
            otel_exporter._send_pending()

        first.export.assert_called_once()
        second.export.assert_called_once()
        self.assertEqual(
            first.export.call_args.args[0], second.export.call_args.args[0]
        )
        mark_sent_many.assert_called_once_with(["0011223344556677"], self.db_path)

    def test_send_pending_leaves_rows_unsent_when_a_destination_returns_failure(self):
        self._insert_pending_row()
        rejecting = MagicMock(name="rejecting")
        rejecting.export.return_value = SpanExportResult.FAILURE
        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._exporters = [("railway", rejecting)]
            otel_exporter._send_pending()

        rejecting.export.assert_called_once()
        self._assert_row_unsent()

    def test_send_pending_tries_every_destination_and_leaves_rows_unsent_on_error(self):
        self._insert_pending_row()
        failed = MagicMock(name="failed")
        failed.export.side_effect = RuntimeError("destination unavailable")
        remaining = MagicMock(name="remaining")
        remaining.export.return_value = SpanExportResult.SUCCESS
        with (
            patch.object(otel_exporter.db, "DB_PATH", self.db_path),
            patch.object(otel_exporter.db, "mark_sent_many") as mark_sent_many,
        ):
            otel_exporter._exporters = [("railway", failed), ("langfuse", remaining)]
            otel_exporter._send_pending()

        failed.export.assert_called_once()
        remaining.export.assert_called_once()
        mark_sent_many.assert_not_called()
        self._assert_row_unsent()

    def test_exporter_construction_failure_shuts_down_already_built_exporters(self):
        first_exporter = MagicMock(name="first_exporter")
        destinations = self._destination_config()
        with (
            patch.object(
                otel_exporter,
                "GrpcOTLPSpanExporter",
                return_value=first_exporter,
            ),
            patch.object(
                otel_exporter,
                "HttpOTLPSpanExporter",
                side_effect=RuntimeError("bad HTTP exporter"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "bad HTTP exporter"):
                otel_exporter._build_exporters(json.dumps(destinations))

        first_exporter.shutdown.assert_called_once_with()


class OTelExporterReloadTests(unittest.TestCase):
    """Redis-backed live reload: each poll tick re-reads otel:destinations and
    rebuilds _exporters only when it changed."""

    def setUp(self):
        self._orig_redis = otel_exporter._redis
        self._orig_raw = otel_exporter._last_destinations_raw
        self._orig_exporters = otel_exporter._exporters
        self.store = {}

        class FakeRedis:
            def get(_self, key):
                return self.store.get(key)

        otel_exporter._redis = FakeRedis()
        otel_exporter._last_destinations_raw = None
        otel_exporter._exporters = []

    def tearDown(self):
        otel_exporter._redis = self._orig_redis
        otel_exporter._last_destinations_raw = self._orig_raw
        otel_exporter._exporters = self._orig_exporters

    def test_reload_builds_exporters_from_redis_on_first_read(self):
        destinations = self._config_for("a", "grpc")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(destinations)
        with patch.object(
            otel_exporter, "GrpcOTLPSpanExporter", return_value=MagicMock(name="e")
        ):
            otel_exporter._reload_destinations_if_changed()
        self.assertEqual([name for name, _ in otel_exporter._exporters], ["a"])

    def test_reload_is_a_noop_when_redis_value_is_unchanged(self):
        destinations = self._config_for("a", "grpc")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(destinations)
        with patch.object(
            otel_exporter, "GrpcOTLPSpanExporter", return_value=MagicMock(name="e")
        ) as exporter_ctor:
            otel_exporter._reload_destinations_if_changed()
            otel_exporter._reload_destinations_if_changed()
        exporter_ctor.assert_called_once()

    def test_reload_rebuilds_and_shuts_down_old_exporters_when_redis_value_changes(
        self,
    ):
        old_exporter = MagicMock(name="old")
        new_exporter = MagicMock(name="new")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(
            self._config_for("a", "grpc")
        )
        with patch.object(
            otel_exporter, "GrpcOTLPSpanExporter", return_value=old_exporter
        ):
            otel_exporter._reload_destinations_if_changed()

        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(
            self._config_for("b", "http")
        )
        with patch.object(
            otel_exporter, "HttpOTLPSpanExporter", return_value=new_exporter
        ):
            otel_exporter._reload_destinations_if_changed()

        old_exporter.shutdown.assert_called_once_with()
        self.assertEqual([name for name, _ in otel_exporter._exporters], ["b"])

    def test_reload_keeps_previous_exporters_when_new_redis_value_is_invalid(self):
        good = MagicMock(name="good")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(
            self._config_for("a", "grpc")
        )
        with patch.object(otel_exporter, "GrpcOTLPSpanExporter", return_value=good):
            otel_exporter._reload_destinations_if_changed()

        self.store[otel_exporter.DESTINATIONS_KEY] = "not json"
        otel_exporter._reload_destinations_if_changed()

        good.shutdown.assert_not_called()
        self.assertEqual([name for name, _ in otel_exporter._exporters], ["a"])

    @staticmethod
    def _config_for(name, protocol):
        return [{"name": name, "protocol": protocol, "endpoint": "host:1"}]


class OTelExporterQuietFailureTests(unittest.TestCase):
    """Failures sqlite and the pricing lookups cannot surface on their own."""

    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.db_file.name
        self.db_file.close()
        db.init_db(self.db_path)
        self._orig_exporters = otel_exporter._exporters
        otel_exporter._consecutive_empty_polls = 0
        otel_exporter._empty_queue_warned = False
        db._cost_failures_logged.clear()
        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS
        otel_exporter._exporters = [("live", exporter)]

    def tearDown(self):
        otel_exporter._exporters = self._orig_exporters
        otel_exporter._consecutive_empty_polls = 0
        otel_exporter._empty_queue_warned = False
        db._cost_failures_logged.clear()
        os.unlink(self.db_path)

    def _poll(self, times):
        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            for _ in range(times):
                otel_exporter._send_pending()

    def _insert(self, future_id, sent):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES (?, ?, 1.0, 2.0, 0, 'A.b', ?)",
                (future_id, "ffeeddccbbaa99887766554433221100", sent),
            )
            conn.commit()
        finally:
            conn.close()

    def test_queue_that_never_receives_rows_warns_once_and_names_the_path(self):
        # sqlite creates a missing file rather than failing, so a misdirected
        # DB_PATH is indistinguishable from an idle queue without this warning.
        with self.assertLogs(otel_exporter.logger, level="WARNING") as captured:
            self._poll(otel_exporter.EMPTY_QUEUE_WARNING_POLLS + 4)

        warnings = [m for m in captured.output if "No rows have ever appeared" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.db_path, warnings[0])

    def test_no_warning_before_the_threshold(self):
        with self.assertNoLogs(otel_exporter.logger, level="WARNING"):
            self._poll(otel_exporter.EMPTY_QUEUE_WARNING_POLLS - 1)

    def test_fully_exported_queue_does_not_warn(self):
        # An empty pending set means "everything is delivered" here, not "wrong
        # database" -- the row count is what separates the two.
        self._insert("0011223344556677", sent=1)
        with self.assertNoLogs(otel_exporter.logger, level="WARNING"):
            self._poll(otel_exporter.EMPTY_QUEUE_WARNING_POLLS + 4)

    def test_cost_lookup_failure_is_logged_once_per_kind_and_keeps_the_row(self):
        rows = [
            {
                "future_id": "0011223344556677",
                "request_id": "r1",
                "agent": "a1",
                "created_at": 1.0,
                "finished_at": 2.0,
                "model": "m",
                "input_token_count": 1,
                "output_token_count": 1,
                "token_count": 2,
            }
        ]
        boom = RuntimeError("no aws_instance_pricing table")
        with (
            patch.object(db.pricing, "compute_token_cost", side_effect=boom),
            patch.object(db.pricing, "compute_server_cost", side_effect=boom),
            self.assertLogs(db.logger, level="WARNING") as captured,
        ):
            for index in range(3):
                rows[0]["future_id"] = f"001122334455667{index}"
                db.write_waiting_rows(rows, None, "proj", db_path=self.db_path)

        self.assertEqual(
            len([m for m in captured.output if "Token cost lookup failed" in m]), 1
        )
        self.assertEqual(
            len([m for m in captured.output if "Server cost lookup failed" in m]), 1
        )

        conn = sqlite3.connect(self.db_path)
        try:
            written, cost = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_cost), 0) FROM waiting"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(written, 3)
        self.assertEqual(cost, 0)


class OTelExporterDeliveryIntegrityTests(unittest.TestCase):
    """Rows that cannot be delivered must not block or be reported as delivered."""

    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.db_file.name
        self.db_file.close()
        db.init_db(self.db_path)
        self._orig_exporters = otel_exporter._exporters
        self._orig_recorders = dict(otel_exporter._partial_success_recorders)

    def tearDown(self):
        otel_exporter._exporters = self._orig_exporters
        otel_exporter._partial_success_recorders.clear()
        otel_exporter._partial_success_recorders.update(self._orig_recorders)
        os.unlink(self.db_path)

    def _insert(self, future_id):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES (?, ?, 1.0, 2.0, 0, 'A.b', 0)",
                (future_id, "ffeeddccbbaa99887766554433221100"),
            )
            conn.commit()
        finally:
            conn.close()

    def _sent(self, future_id):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT sent FROM waiting WHERE future_id = ?", (future_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    def test_one_unexportable_row_does_not_block_the_rest_of_the_batch(self):
        good = ["0011223344556677", "1122334455667788"]
        poison = "00112233445566778899aabbccddeeff"  # 128-bit, overflows span_id
        for future_id in good:
            self._insert(future_id)
        self._insert(poison)

        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS
        otel_exporter._exporters = [("live", exporter)]
        with (
            patch.object(otel_exporter.db, "DB_PATH", self.db_path),
            patch.object(otel_exporter.db, "mark_sent_many") as mark_sent_many,
        ):
            otel_exporter._send_pending()

        self.assertEqual(len(exporter.export.call_args.args[0]), len(good))
        mark_sent_many.assert_called_once_with(good, self.db_path)

    def test_unexportable_row_is_named_in_the_log(self):
        poison = "00112233445566778899aabbccddeeff"
        self._insert(poison)
        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS
        otel_exporter._exporters = [("live", exporter)]

        with (
            patch.object(otel_exporter.db, "DB_PATH", self.db_path),
            self.assertLogs(otel_exporter.logger, level="ERROR") as captured,
        ):
            otel_exporter._send_pending()

        self.assertTrue(any(poison in line for line in captured.output))
        exporter.export.assert_not_called()

    def test_partial_success_rejection_leaves_the_batch_unsent(self):
        # The SDK returns SUCCESS for a 200 that rejected spans, so delivery is
        # only known by reading partial_success off the response.
        future_id = "0011223344556677"
        self._insert(future_id)
        recorder = otel_exporter._PartialSuccessRecorder("live")

        def export_with_partial_rejection(spans):
            # The real hook fires during the HTTP call, after _send_pending has
            # reset the recorder, so the mock has to populate it the same way.
            recorder.rejected_spans = 1
            recorder.error_message = "span rejected"
            return SpanExportResult.SUCCESS

        exporter = MagicMock(name="exporter")
        exporter.export.side_effect = export_with_partial_rejection
        otel_exporter._exporters = [("live", exporter)]
        otel_exporter._partial_success_recorders.clear()
        otel_exporter._partial_success_recorders["live"] = recorder

        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._send_pending()

        self.assertEqual(self._sent(future_id), 0)

    def test_recorder_reads_rejected_spans_off_an_ok_response(self):
        response = ExportTraceServiceResponse()
        response.partial_success.rejected_spans = 2
        response.partial_success.error_message = "two bad spans"
        http_response = MagicMock(ok=True, content=response.SerializeToString())

        recorder = otel_exporter._PartialSuccessRecorder("live")
        recorder(http_response)

        self.assertEqual(recorder.rejected_spans, 2)
        self.assertEqual(recorder.error_message, "two bad spans")


class OTelExporterConfigValidationTests(unittest.TestCase):
    def test_unsupported_protocol_is_rejected_instead_of_defaulting_to_http(self):
        for protocol in ("grcp", None, "", 7):
            with self.subTest(protocol=protocol):
                with self.assertRaisesRegex(ValueError, "protocol must be one of"):
                    otel_exporter._configured_destinations(
                        json.dumps(
                            [{"name": "a", "protocol": protocol, "endpoint": "h:1"}]
                        )
                    )

    def test_protocol_case_is_normalized(self):
        for given, expected in (
            ("HTTP", "http"),
            ("GRPC", "grpc"),
            ("Http/Protobuf", "http/protobuf"),
        ):
            with self.subTest(protocol=given):
                parsed = otel_exporter._configured_destinations(
                    json.dumps([{"name": "a", "protocol": given, "endpoint": "h:1"}])
                )
                self.assertEqual(parsed[0]["protocol"], expected)

    def test_supported_protocols_are_accepted(self):
        for protocol in otel_exporter.SUPPORTED_PROTOCOLS:
            with self.subTest(protocol=protocol):
                parsed = otel_exporter._configured_destinations(
                    json.dumps([{"name": "a", "protocol": protocol, "endpoint": "h:1"}])
                )
                self.assertEqual(parsed[0]["protocol"], protocol)


class OTelExporterRowFidelityTests(unittest.TestCase):
    def test_missing_identifiers_are_logged_not_silently_dropped(self):
        with self.assertLogs(db.logger, level="WARNING") as captured:
            db.write_waiting_rows(
                [{"future_id": "0011223344556677", "request_id": ""}],
                None,
                "proj",
                db_path=":memory:",
            )
        self.assertTrue(any("request_id" in line for line in captured.output))

    def test_absent_error_name_does_not_fabricate_an_exception_type(self):
        span = convert.waiting_row_to_span(
            {
                "future_id": "0011223344556677",
                "session_id": "ffeeddccbbaa99887766554433221100",
                "parent_id": None,
                "failed": 1,
                "error_name": None,
                "error_message": "timed out",
                "name": "A.b",
                "started_at": 1.0,
                "finished_at": 2.0,
            }
        )
        self.assertNotIn("exception.type", span.events[0].attributes)
        self.assertEqual(span.events[0].attributes["exception.message"], "timed out")

    def test_empty_output_is_preserved_rather_than_treated_as_absent(self):
        self.assertEqual(db._normalize_json_text(""), '""')
        self.assertIsNone(db._normalize_json_text(None))


class OTelExporterPlaceholderTests(unittest.TestCase):
    """A row that can never be encoded is retired, not retried forever."""

    def setUp(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.db_file.name
        self.db_file.close()
        db.init_db(self.db_path)
        self._orig_exporters = otel_exporter._exporters
        otel_exporter._row_export_failures.clear()
        self.poison = "00112233445566778899aabbccddeeff"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES (?, ?, 1.0, 2.0, 0, 'A.b', 0)",
                (self.poison, "ffeeddccbbaa99887766554433221100"),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        otel_exporter._exporters = self._orig_exporters
        otel_exporter._row_export_failures.clear()
        os.unlink(self.db_path)

    def _poll_once(self, exporter):
        otel_exporter._exporters = [("live", exporter)]
        with patch.object(otel_exporter.db, "DB_PATH", self.db_path):
            otel_exporter._send_pending()

    def test_placeholder_is_sent_only_after_the_attempt_limit(self):
        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS

        for _ in range(otel_exporter.MAX_ROW_EXPORT_ATTEMPTS - 1):
            self._poll_once(exporter)
        exporter.export.assert_not_called()

        self._poll_once(exporter)
        sent_spans = exporter.export.call_args.args[0]
        self.assertEqual(len(sent_spans), 1)
        self.assertEqual(sent_spans[0].name, "canyonos.invalid_span")

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT sent FROM waiting").fetchone()[0], 1)
        finally:
            conn.close()

    def test_export_failures_do_not_count_toward_the_row_limit(self):
        # Otherwise a spell of receiver downtime would replace every queued row
        # with a placeholder.
        good = "1122334455667788"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM waiting")
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES (?, ?, 1.0, 2.0, 0, 'A.b', 0)",
                (good, "ffeeddccbbaa99887766554433221100"),
            )
            conn.commit()
        finally:
            conn.close()

        down = MagicMock(name="down")
        down.export.return_value = SpanExportResult.FAILURE
        for _ in range(otel_exporter.MAX_ROW_EXPORT_ATTEMPTS + 3):
            self._poll_once(down)

        self.assertNotIn(good, otel_exporter._row_export_failures)
        recovered = MagicMock(name="recovered")
        recovered.export.return_value = SpanExportResult.SUCCESS
        self._poll_once(recovered)
        self.assertEqual(recovered.export.call_args.args[0][0].name, "A.b")

    def test_placeholder_is_not_disguised_as_an_agent_failure(self):
        session_id = "ffeeddccbbaa99887766554433221100"
        placeholder = convert.invalid_row_placeholder_span(
            {
                "future_id": self.poison,
                "session_id": session_id,
                "started_at": 1.0,
                "finished_at": 2.0,
            },
            "int too big to convert",
        )

        self.assertEqual(placeholder.name, "canyonos.invalid_span")
        self.assertIs(placeholder.attributes["canyonos.export.invalid"], True)
        self.assertEqual(placeholder.attributes["canyonos.future_id"], self.poison)
        self.assertEqual(placeholder.events, ())
        self.assertEqual(placeholder.context.trace_id, int(session_id, 16))
        self.assertTrue(0 < placeholder.context.span_id <= 2**64 - 1)


class OTelExporterLifecycleLoggingTests(unittest.TestCase):
    """Connectivity and recovery must be visible without waiting for traffic."""

    def setUp(self):
        self._orig_exporters = otel_exporter._exporters
        otel_exporter._destination_healthy.clear()

    def tearDown(self):
        otel_exporter._exporters = self._orig_exporters
        otel_exporter._destination_healthy.clear()

    def test_reachable_destination_is_reported_at_build_time(self):
        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS
        with self.assertLogs(otel_exporter.logger, level="INFO") as captured:
            otel_exporter._probe_destination("live", exporter)
        exporter.export.assert_called_once_with([])
        self.assertTrue(
            any("answered a connectivity check" in line for line in captured.output)
        )

    def test_unreachable_destination_is_reported_at_build_time(self):
        exporter = MagicMock(name="exporter")
        exporter.export.side_effect = RuntimeError("connection refused")
        with self.assertLogs(otel_exporter.logger, level="WARNING") as captured:
            otel_exporter._probe_destination("dead", exporter)
        self.assertTrue(
            any(
                "did not answer a connectivity check" in line
                for line in captured.output
            )
        )
        self.assertTrue(any("connection refused" in line for line in captured.output))

    def test_recovery_is_logged_only_after_a_failure(self):
        db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = db_file.name
        db_file.close()
        self.addCleanup(os.unlink, db_path)
        db.init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES ('0011223344556677',"
                " 'ffeeddccbbaa99887766554433221100', 1.0, 2.0, 0, 'A.b', 0)"
            )
            conn.commit()
        finally:
            conn.close()

        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.FAILURE
        otel_exporter._exporters = [("live", exporter)]
        with patch.object(otel_exporter.db, "DB_PATH", db_path):
            otel_exporter._send_pending()
            self.assertIs(otel_exporter._destination_healthy["live"], False)

            exporter.export.return_value = SpanExportResult.SUCCESS
            with self.assertLogs(otel_exporter.logger, level="INFO") as captured:
                otel_exporter._send_pending()

        self.assertTrue(
            any("is accepting spans again" in line for line in captured.output)
        )

    def test_steady_success_does_not_log_recovery(self):
        db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = db_file.name
        db_file.close()
        self.addCleanup(os.unlink, db_path)
        db.init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO waiting (future_id, session_id, started_at, finished_at,"
                " failed, name, sent) VALUES ('0011223344556677',"
                " 'ffeeddccbbaa99887766554433221100', 1.0, 2.0, 0, 'A.b', 0)"
            )
            conn.commit()
        finally:
            conn.close()

        exporter = MagicMock(name="exporter")
        exporter.export.return_value = SpanExportResult.SUCCESS
        otel_exporter._destination_healthy["live"] = True
        otel_exporter._exporters = [("live", exporter)]

        with (
            patch.object(otel_exporter.db, "DB_PATH", db_path),
            self.assertLogs(otel_exporter.logger, level="INFO") as captured,
        ):
            otel_exporter._send_pending()

        self.assertTrue(any("Exported 1 span(s)" in line for line in captured.output))
        self.assertFalse(
            any("is accepting spans again" in line for line in captured.output)
        )


if __name__ == "__main__":
    unittest.main()
