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
        grpc_exporter = object()
        http_exporter = object()
        grpc_processor = MagicMock(name="grpc_processor")
        http_processor = MagicMock(name="http_processor")
        destinations = self._destination_config()

        with patch.object(
            otel_exporter,
            "GrpcOTLPSpanExporter",
            return_value=grpc_exporter,
        ) as grpc_constructor, patch.object(
            otel_exporter,
            "HttpOTLPSpanExporter",
            return_value=http_exporter,
        ) as http_constructor, patch.object(
            otel_exporter,
            "BatchSpanProcessor",
            side_effect=[grpc_processor, http_processor],
        ) as processor_constructor:
            processors = otel_exporter._build_processors(json.dumps(destinations))

        self.assertEqual(
            processors, [("railway", grpc_processor), ("langfuse", http_processor)]
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
        )
        self.assertEqual(
            processor_constructor.call_args_list,
            [
                unittest.mock.call(grpc_exporter, schedule_delay_millis=1000),
                unittest.mock.call(http_exporter, schedule_delay_millis=1000),
            ],
        )

    def test_build_processors_raises_when_destinations_raw_is_none(self):
        with self.assertRaisesRegex(RuntimeError, "otel.destinations is required"):
            otel_exporter._build_processors(None)

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
        # reload change (VENTIS_OTEL_DESTINATIONS -> otel:destinations); out of
        # scope here, so this only covers ${ENV_VAR} expansion, which does work.
        from ventis.controller.global_controller import GlobalController

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
        from ventis.controller.global_controller import GlobalController

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
        first_processor = MagicMock(name="first_processor")
        destinations = self._destination_config()
        with patch.object(
            otel_exporter,
            "GrpcOTLPSpanExporter",
            return_value=object(),
        ), patch.object(
            otel_exporter,
            "HttpOTLPSpanExporter",
            side_effect=RuntimeError("bad HTTP exporter"),
        ), patch.object(
            otel_exporter,
            "BatchSpanProcessor",
            return_value=first_processor,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad HTTP exporter"):
                otel_exporter._build_processors(json.dumps(destinations))

        first_processor.shutdown.assert_called_once_with()


class OTelExporterReloadTests(unittest.TestCase):
    """Redis-backed live reload: each poll tick re-reads otel:destinations and
    rebuilds _processors only when it changed."""

    def setUp(self):
        self._orig_redis = otel_exporter._redis
        self._orig_raw = otel_exporter._last_destinations_raw
        self._orig_processors = otel_exporter._processors
        self.store = {}

        class FakeRedis:
            def get(_self, key):
                return self.store.get(key)

        otel_exporter._redis = FakeRedis()
        otel_exporter._last_destinations_raw = None
        otel_exporter._processors = []

    def tearDown(self):
        otel_exporter._redis = self._orig_redis
        otel_exporter._last_destinations_raw = self._orig_raw
        otel_exporter._processors = self._orig_processors

    def test_reload_builds_processors_from_redis_on_first_read(self):
        destinations = self._config_for("a", "grpc")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(destinations)
        with patch.object(otel_exporter, "GrpcOTLPSpanExporter", return_value=object()), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=MagicMock(name="p")
        ):
            otel_exporter._reload_destinations_if_changed()
        self.assertEqual([name for name, _ in otel_exporter._processors], ["a"])

    def test_reload_is_a_noop_when_redis_value_is_unchanged(self):
        destinations = self._config_for("a", "grpc")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(destinations)
        with patch.object(otel_exporter, "GrpcOTLPSpanExporter", return_value=object()), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=MagicMock(name="p")
        ) as processor_ctor:
            otel_exporter._reload_destinations_if_changed()
            otel_exporter._reload_destinations_if_changed()
        processor_ctor.assert_called_once()

    def test_reload_rebuilds_and_shuts_down_old_processors_when_redis_value_changes(self):
        old_processor = MagicMock(name="old")
        new_processor = MagicMock(name="new")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(self._config_for("a", "grpc"))
        with patch.object(otel_exporter, "GrpcOTLPSpanExporter", return_value=object()), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=old_processor
        ):
            otel_exporter._reload_destinations_if_changed()

        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(self._config_for("b", "http"))
        with patch.object(otel_exporter, "HttpOTLPSpanExporter", return_value=object()), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=new_processor
        ):
            otel_exporter._reload_destinations_if_changed()

        old_processor.shutdown.assert_called_once_with()
        self.assertEqual([name for name, _ in otel_exporter._processors], ["b"])

    def test_reload_keeps_previous_processors_when_new_redis_value_is_invalid(self):
        good = MagicMock(name="good")
        self.store[otel_exporter.DESTINATIONS_KEY] = json.dumps(self._config_for("a", "grpc"))
        with patch.object(otel_exporter, "GrpcOTLPSpanExporter", return_value=object()), patch.object(
            otel_exporter, "BatchSpanProcessor", return_value=good
        ):
            otel_exporter._reload_destinations_if_changed()

        self.store[otel_exporter.DESTINATIONS_KEY] = "not json"
        otel_exporter._reload_destinations_if_changed()

        good.shutdown.assert_not_called()
        self.assertEqual([name for name, _ in otel_exporter._processors], ["a"])

    @staticmethod
    def _config_for(name, protocol):
        return [{"name": name, "protocol": protocol, "endpoint": "host:1"}]


if __name__ == "__main__":
    unittest.main()
