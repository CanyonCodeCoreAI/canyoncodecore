"""Tests for the OTel *metrics* pipeline: db.metric_write_rows, metric_convert,
the per-signal HTTP endpoint, the metric exporter build, _metric_send_pending, and the
metrics-specific empty-queue tracker. (The trace side is covered by
test_otel_exporter_fanout.py / test_otel_telemetry_pull.py.)
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

from opentelemetry.sdk.metrics.export import (  # noqa: E402
    AggregationTemporality,
    Gauge,
    MetricExportResult,
    MetricsData,
    Sum,
)

import db  # noqa: E402
import metric_convert  # noqa: E402
import otel_exporter  # noqa: E402

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


_AGENT_METRICS = {
    "status": "healthy",
    "queue_length": "3",
    "observed_at": "100.0",
    "started_at": "50.0",
    "requests_served": "42",
    "full_failures": "2",
}


class MetricConvertAgentTests(unittest.TestCase):
    def _convert(self, **overrides):
        row = {
            "kind": "agent",
            "agent_id": "a1",
            "agent_name": "myagent",
            "host": "h1",
            "port": "50051",
            "project_id": "proj",
            "observed_at": 100.0,
            "metrics": json.dumps({**_AGENT_METRICS, **overrides}),
        }
        return metric_convert.metric_row_to_resource_metrics(row)

    def test_resource_identifies_the_agent(self):
        attrs = dict(self._convert().resource.attributes)
        self.assertEqual(attrs["service.name"], "myagent")
        self.assertEqual(attrs["service.instance.id"], "a1")
        self.assertEqual(attrs["host.name"], "h1")
        self.assertEqual(attrs["canyonos.agent.port"], "50051")
        self.assertEqual(attrs["canyonos.project.id"], "proj")

    def test_queue_length_is_a_gauge(self):
        by = {m.name: m for m in self._convert().scope_metrics[0].metrics}
        q = by["canyonos.agent.queue.length"]
        self.assertIsInstance(q.data, Gauge)
        self.assertEqual(q.data.data_points[0].value, 3)

    def test_counters_are_monotonic_cumulative_sums_with_start_time(self):
        by = {m.name: m for m in self._convert().scope_metrics[0].metrics}
        reqs = by["canyonos.agent.requests"]
        dp = reqs.data.data_points[0]
        self.assertIsInstance(reqs.data, Sum)
        self.assertTrue(reqs.data.is_monotonic)
        self.assertEqual(
            reqs.data.aggregation_temporality, AggregationTemporality.CUMULATIVE
        )
        self.assertEqual(dp.value, 42)
        # start = started_at (50s), point time = observed_at (100s)
        self.assertEqual(dp.start_time_unix_nano, int(50.0 * 1e9))
        self.assertEqual(dp.time_unix_nano, int(100.0 * 1e9))
        self.assertEqual(by["canyonos.agent.failures"].data.data_points[0].value, 2)

    def test_health_gauge_reflects_status(self):
        up = {m.name: m for m in self._convert().scope_metrics[0].metrics}[
            "canyonos.agent.up"
        ]
        self.assertEqual(up.data.data_points[0].value, 1)
        down = {
            m.name: m
            for m in self._convert(status="stopped").scope_metrics[0].metrics
        }["canyonos.agent.up"]
        self.assertEqual(down.data.data_points[0].value, 0)


class MetricConvertMachineAndDispatchTests(unittest.TestCase):
    def test_machine_row_builds_machine_resource_and_gauges(self):
        row = {
            "kind": "machine",
            "host": "h1",
            "project_id": "proj",
            "observed_at": 9.0,
            "metrics": json.dumps({"cpu_percent": "5.0", "observed_at": "9.0"}),
        }
        rm = metric_convert.metric_row_to_resource_metrics(row)
        self.assertEqual(dict(rm.resource.attributes)["service.name"], "canyonos-machine-h1")
        self.assertEqual(
            {m.name for m in rm.scope_metrics[0].metrics},
            {"canyonos.machine.cpu.utilization"},
        )

    def test_empty_or_unparseable_metrics_return_none(self):
        for blob in ("{}", "not-json"):
            row = {"kind": "agent", "host": "h", "observed_at": 1.0, "metrics": blob}
            self.assertIsNone(metric_convert.metric_row_to_resource_metrics(row))


class WriteMetricsRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _rows(self):
        with sqlite3.connect(self.tmp) as conn:
            cols = "sample_id,kind,agent_id,agent_name,host,port,observed_at"
            return {r[0]: r for r in conn.execute(f"SELECT {cols} FROM metrics_waiting")}

    def test_agent_and_machine_rows_get_distinct_deterministic_ids(self):
        db.metric_write_rows(
            [{"kind": "agent", "agent_id": "a1", "agent_name": "n", "host": "h1",
              "port": 50051, "project_id": "p", "metrics": _AGENT_METRICS}],
            self.tmp,
        )
        db.metric_write_rows(
            [{"kind": "machine", "host": "h1", "project_id": "p",
              "metrics": {"cpu_percent": "5", "observed_at": "9.0"}}],
            self.tmp,
        )
        rows = self._rows()
        # agent id from agent_id + observed_at; machine id from host + observed_at
        self.assertIn("agent:a1:100000000000", rows)
        self.assertIn("machine:h1:9000000000", rows)
        inst = rows["agent:a1:100000000000"]
        self.assertEqual((inst[1], inst[2], inst[4], inst[5], inst[6]),
                         ("agent", "a1", "h1", "50051", 100.0))

    def test_repolling_the_same_tick_upserts_instead_of_duplicating(self):
        row = {"kind": "agent", "agent_id": "a1", "agent_name": "n", "host": "h1",
               "port": 50051, "project_id": "p", "metrics": _AGENT_METRICS}
        db.metric_write_rows([row], self.tmp)
        db.metric_write_rows([row], self.tmp)
        with sqlite3.connect(self.tmp) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM metrics_waiting").fetchone()[0], 1
            )

    def test_one_bad_row_does_not_drop_the_rest_of_the_batch(self):
        good = {"kind": "machine", "host": "h1", "metrics": {"observed_at": "1.0"}}
        bad = {"kind": "machine", "host": None, "metrics": None}  # skipped
        db.metric_write_rows([bad, good], self.tmp)
        self.assertEqual(len(self._rows()), 1)


def _destination(protocol, endpoint):
    return otel_exporter._configured_destinations(
        json.dumps([{"name": "d", "protocol": protocol, "endpoint": endpoint}])
    )[0]


class SignalEndpointTests(unittest.TestCase):
    def test_http_trace_exporter_targets_v1_traces(self):
        with patch.object(
            otel_exporter, "HttpOTLPSpanExporter", return_value=object()
        ) as ctor:
            otel_exporter._trace_build_exporter(_destination("http", "https://x/api/otel"))
        self.assertEqual(
            ctor.call_args.kwargs["endpoint"], "https://x/api/otel/v1/traces"
        )

    def test_http_metric_exporter_targets_v1_metrics(self):
        with patch.object(
            otel_exporter, "HttpOTLPMetricExporter", return_value=object()
        ) as ctor:
            otel_exporter._metric_build_exporter(_destination("http", "http://host:4318"))
        self.assertEqual(ctor.call_args.kwargs["endpoint"], "http://host:4318/v1/metrics")

    def test_grpc_metric_exporter_keeps_the_bare_endpoint(self):
        with patch.object(
            otel_exporter, "GrpcOTLPMetricExporter", return_value=object()
        ) as ctor:
            otel_exporter._metric_build_exporter(_destination("grpc", "host:4317"))
        self.assertEqual(ctor.call_args.kwargs["endpoint"], "host:4317")


class SendPendingMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)
        self._orig = otel_exporter._metric_exporters

    def tearDown(self):
        otel_exporter._metric_exporters = self._orig
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed(self, metrics=_AGENT_METRICS, sample_kind="agent"):
        db.metric_write_rows(
            [{"kind": sample_kind, "agent_id": "a1", "agent_name": "n", "host": "h1",
              "port": 50051, "project_id": "p", "metrics": metrics}],
            self.tmp,
        )

    def _sent(self):
        with sqlite3.connect(self.tmp) as conn:
            return conn.execute("SELECT sent FROM metrics_waiting").fetchone()[0]

    def test_success_exports_metricsdata_and_marks_sent(self):
        self._seed()
        exporter = MagicMock(name="live")
        exporter.export.return_value = MetricExportResult.SUCCESS
        otel_exporter._metric_exporters = [("live", exporter)]
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            otel_exporter._metric_send_pending()
        exporter.export.assert_called_once()
        self.assertIsInstance(exporter.export.call_args.args[0], MetricsData)
        self.assertEqual(self._sent(), 1)

    def test_failure_leaves_the_sample_unsent(self):
        self._seed()
        exporter = MagicMock(name="live")
        exporter.export.return_value = MetricExportResult.FAILURE
        otel_exporter._metric_exporters = [("live", exporter)]
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            otel_exporter._metric_send_pending()
        self.assertEqual(self._sent(), 0)

    def test_unparseable_sample_is_marked_done_without_exporting(self):
        self._seed(metrics={"observed_at": "1.0"})  # no gauges/counters -> converts to None
        exporter = MagicMock(name="live")
        exporter.export.return_value = MetricExportResult.SUCCESS
        otel_exporter._metric_exporters = [("live", exporter)]
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            otel_exporter._metric_send_pending()
        exporter.export.assert_not_called()
        self.assertEqual(self._sent(), 1)


class MetricQueueStateTests(unittest.TestCase):
    """The metrics empty-queue tracker is independent of the trace one."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)
        self._orig = otel_exporter._metric_exporters
        self._orig_trace = dict(otel_exporter._trace_empty_queue)
        self._orig_metric = dict(otel_exporter._metric_empty_queue)
        otel_exporter._trace_empty_queue = {"consecutive": 0, "warned_at": None}
        otel_exporter._metric_empty_queue = {"consecutive": 0, "warned_at": None}
        exporter = MagicMock(name="live")
        exporter.export.return_value = MetricExportResult.SUCCESS
        otel_exporter._metric_exporters = [("live", exporter)]

    def tearDown(self):
        otel_exporter._metric_exporters = self._orig
        otel_exporter._trace_empty_queue = self._orig_trace
        otel_exporter._metric_empty_queue = self._orig_metric
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def test_empty_metrics_queue_warns_once_and_leaves_trace_tracker_untouched(self):
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            with self.assertLogs(otel_exporter.logger, level="WARNING") as captured:
                for _ in range(otel_exporter.EMPTY_QUEUE_WARNING_POLLS + 3):
                    otel_exporter._metric_send_pending()
        warnings = [m for m in captured.output if "No metrics samples have ever appeared" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.tmp, warnings[0])
        # The trace tracker never advanced -- the two signals count separately.
        self.assertEqual(otel_exporter._trace_empty_queue["consecutive"], 0)


class PruneExpiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed_metric(self, sample_id, observed_at, sent):
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO metrics_waiting (sample_id, kind, host, observed_at, "
                "metrics, sent) VALUES (?, 'machine', 'h', ?, '{}', ?)",
                (sample_id, observed_at, sent),
            )
            conn.commit()

    def _metric_ids(self):
        with sqlite3.connect(self.tmp) as conn:
            return {r[0] for r in conn.execute("SELECT sample_id FROM metrics_waiting")}

    def test_prune_removes_aged_out_rows_regardless_of_sent(self):
        now = time.time()
        self._seed_metric("old-sent", now - 10000, 1)     # removed
        self._seed_metric("old-unsent", now - 10000, 0)   # removed: sent-agnostic
        self._seed_metric("new-unsent", now - 10, 0)      # kept: too recent
        removed = db.prune_expired("metrics_waiting", "observed_at", 600, self.tmp)
        self.assertEqual(removed, 2)
        self.assertEqual(self._metric_ids(), {"new-unsent"})

    def test_prune_leaves_rows_with_a_null_timestamp(self):
        # NULL finished_at is how an in-flight span looks; the age-out must never drop
        # those mid-execution, even when unsent.
        self._seed_metric("unsent-null-ts", None, 0)
        self.assertEqual(db.prune_expired("metrics_waiting", "observed_at", 0, self.tmp), 0)
        self.assertEqual(self._metric_ids(), {"unsent-null-ts"})

    def test_prune_expired_rows_covers_both_tables(self):
        now = time.time()
        self._seed_metric("old", now - otel_exporter.METRIC_RETENTION_SECONDS - 60, 1)
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO traces_waiting (future_id, session_id, started_at, finished_at, "
                "sent) VALUES ('f1', 's1', 1.0, ?, 1)",
                (now - otel_exporter.TRACE_RETENTION_SECONDS - 60,),
            )
            conn.commit()
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            otel_exporter._prune_expired_rows()
        with sqlite3.connect(self.tmp) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM metrics_waiting").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM traces_waiting").fetchone()[0], 0
            )


class FlushModeTests(unittest.TestCase):
    """With no OTel destination configured, the exporter flushes the queue each poll."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".db")
        db.init_db(self.tmp)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _seed_both(self):
        with sqlite3.connect(self.tmp) as conn:
            conn.execute(
                "INSERT INTO metrics_waiting (sample_id, kind, host, observed_at, "
                "metrics, sent) VALUES ('m-unsent','machine','h',1.0,'{}',0)"
            )
            conn.execute(
                "INSERT INTO metrics_waiting (sample_id, kind, host, observed_at, "
                "metrics, sent) VALUES ('m-sent','machine','h',1.0,'{}',1)"
            )
            conn.execute(
                "INSERT INTO traces_waiting (future_id, session_id, started_at, finished_at, "
                "sent) VALUES ('f1','s1',1.0,2.0,0)"
            )
            conn.commit()

    def _counts(self):
        with sqlite3.connect(self.tmp) as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM traces_waiting").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM metrics_waiting").fetchone()[0],
            )

    def test_flush_all_deletes_sent_and_unsent(self):
        self._seed_both()
        removed = db.flush_all("metrics_waiting", self.tmp)
        self.assertEqual(removed, 2)  # both the unsent and the sent row
        self.assertEqual(self._counts()[1], 0)

    def test_flush_pending_targets_only_the_named_table(self):
        self._seed_both()
        with patch.object(otel_exporter.db, "DB_PATH", self.tmp):
            otel_exporter._flush_pending("traces_waiting", "span row(s)")
            # only the traces table is flushed; the metrics table is left untouched
            traces_after, metrics_after = self._counts()
            self.assertEqual(traces_after, 0)
            self.assertGreater(metrics_after, 0)
            otel_exporter._flush_pending("metrics_waiting", "metric sample(s)")
        self.assertEqual(self._counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
