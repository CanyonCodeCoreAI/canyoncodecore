"""Focused tests for the Ventis OTel exporter fan-out configuration."""

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
sys.path.insert(0, os.path.join(ROOT, "ventis", "OTLP_Exporter"))

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

    def test_build_processors_constructs_mixed_exporters_with_explicit_args(self):
        grpc_span_exporter = object()
        http_span_exporter = object()
        grpc_log_exporter = object()
        http_log_exporter = object()
        grpc_span_proc = MagicMock(name="grpc_span_proc")
        http_span_proc = MagicMock(name="http_span_proc")
        grpc_log_proc = MagicMock(name="grpc_log_proc")
        http_log_proc = MagicMock(name="http_log_proc")
        destinations = self._destination_config()

        with patch.dict(
            os.environ,
            {otel_exporter.DESTINATIONS_ENV: json.dumps(destinations)},
            clear=True,
        ), patch.object(
            otel_exporter, "GrpcOTLPSpanExporter", return_value=grpc_span_exporter,
        ), patch.object(
            otel_exporter, "HttpOTLPSpanExporter", return_value=http_span_exporter,
        ), patch.object(
            otel_exporter, "GrpcOTLPLogExporter", return_value=grpc_log_exporter,
        ), patch.object(
            otel_exporter, "HttpOTLPLogExporter", return_value=http_log_exporter,
        ), patch.object(
            otel_exporter, "BatchSpanProcessor",
            side_effect=[grpc_span_proc, http_span_proc],
        ), patch.object(
            otel_exporter, "BatchLogRecordProcessor",
            side_effect=[grpc_log_proc, http_log_proc],
        ):
            span_procs, log_procs = otel_exporter._build_processors()

        self.assertEqual(span_procs, [("railway", grpc_span_proc), ("langfuse", http_span_proc)])
        self.assertEqual(log_procs, [("railway", grpc_log_proc), ("langfuse", http_log_proc)])

    def test_build_processors_raises_when_destinations_env_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "otel.destinations is required"):
                otel_exporter._build_processors()

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
            with self.subTest(raw=raw), patch.dict(
                os.environ, {otel_exporter.DESTINATIONS_ENV: raw}, clear=True
            ):
                with self.assertRaises(ValueError):
                    otel_exporter._configured_destinations()

    def test_controller_expands_env_and_builds_langfuse_basic_auth(self):
        from ventis.controller.global_controller import GlobalController

        with patch.dict(
            os.environ,
            {
                "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
                "LANGFUSE_PUBLIC_KEY": "public",
                "LANGFUSE_SECRET_KEY": "secret",
            },
            clear=True,
        ):
            env = GlobalController._otel_exporter_env(
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

        destination = json.loads(env[otel_exporter.DESTINATIONS_ENV])[0]
        self.assertEqual(
            destination["endpoint"],
            "https://us.cloud.langfuse.com/api/public/otel/v1/traces",
        )
        self.assertEqual(destination["headers"]["Authorization"], "Basic cHVibGljOnNlY3JldA==")

    def test_controller_env_serializes_destinations_only(self):
        # Importing the controller is intentionally local: this test remains
        # runnable in the exporter-only environment used by the focused suite.
        from ventis.controller.global_controller import GlobalController

        destinations = self._destination_config()
        env = GlobalController._otel_exporter_env({"destinations": destinations})
        self.assertEqual(set(env), {otel_exporter.DESTINATIONS_ENV})
        self.assertEqual(json.loads(env[otel_exporter.DESTINATIONS_ENV]), destinations)

    def test_controller_env_is_none_when_otel_not_configured(self):
        from ventis.controller.global_controller import GlobalController

        self.assertIsNone(GlobalController._otel_exporter_env({}))

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
                    "00112233445566778899aabbccddeeff",
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

    def test_send_pending_delivers_the_same_span_to_every_processor(self):
        self._insert_pending_row()
        first = MagicMock(name="first")
        second = MagicMock(name="second")
        with patch.object(otel_exporter.db, "DB_PATH", self.db_path), patch.object(
            otel_exporter.db, "mark_sent"
        ) as mark_sent:
            otel_exporter._processors = [("railway", first), ("langfuse", second)]
            otel_exporter._send_pending()

        first.on_end.assert_called_once()
        second.on_end.assert_called_once()
        self.assertIs(first.on_end.call_args.args[0], second.on_end.call_args.args[0])
        mark_sent.assert_called_once_with("00112233445566778899aabbccddeeff")

    def test_send_pending_attempts_remaining_processors_and_leaves_row_unsent_on_failure(self):
        self._insert_pending_row()
        failed = MagicMock(name="failed")
        failed.on_end.side_effect = RuntimeError("destination unavailable")
        remaining = MagicMock(name="remaining")
        with patch.object(otel_exporter.db, "DB_PATH", self.db_path), patch.object(
            otel_exporter.db, "mark_sent"
        ) as mark_sent:
            otel_exporter._processors = [("railway", failed), ("langfuse", remaining)]
            otel_exporter._send_pending()

        failed.on_end.assert_called_once()
        remaining.on_end.assert_called_once()
        mark_sent.assert_not_called()

        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT sent FROM waiting").fetchone()[0], 0)
        finally:
            conn.close()

    def test_processor_construction_failure_shuts_down_already_built_processors(self):
        first_span_proc = MagicMock(name="first_span_proc")
        first_log_proc = MagicMock(name="first_log_proc")
        destinations = self._destination_config()
        with patch.dict(
            os.environ,
            {otel_exporter.DESTINATIONS_ENV: json.dumps(destinations)},
            clear=True,
        ), patch.object(
            otel_exporter, "GrpcOTLPSpanExporter", return_value=object(),
        ), patch.object(
            otel_exporter, "GrpcOTLPLogExporter", return_value=object(),
        ), patch.object(
            otel_exporter, "HttpOTLPSpanExporter",
            side_effect=RuntimeError("bad HTTP exporter"),
        ), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=first_span_proc,
        ), patch.object(
            otel_exporter, "BatchLogRecordProcessor", return_value=first_log_proc,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad HTTP exporter"):
                otel_exporter._build_processors()

        first_span_proc.shutdown.assert_called_once_with()
        first_log_proc.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
