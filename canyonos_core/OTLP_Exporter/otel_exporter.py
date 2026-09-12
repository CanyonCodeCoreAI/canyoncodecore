"""Entrypoint for the OTLP exporter process."""

import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from canyonos_core.controller.utils.redis_client import RedisClient

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcOTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpOTLPMetricExporter,
)
from opentelemetry.sdk.metrics.export import MetricExportResult, MetricsData
import requests

import convert
import metric_convert
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_running = True
_trace_exporters = []   # (name, OTLPSpanExporter)
_metric_exporters = []  # (name, OTLPMetricExporter)
_last_destinations_raw = None
POLL_INTERVAL_SECONDS = 5
# Bounds one export request, since a backlog is drained by repeated polls. Shared by
# traces and metrics -- one poll drains up to this many trace rows and this many metric
# samples.
MAX_SPANS_PER_POLL = 512
MAX_ROW_EXPORT_ATTEMPTS = 5
# Counted in memory only: a placeholder marks the row sent, so it leaves the
# pending set for good and the count never needs to survive a restart.
_row_export_failures = {}
EMPTY_QUEUE_WARNING_POLLS = 12
EMPTY_QUEUE_REWARN_POLLS = 720
SUPPORTED_PROTOCOLS = ("grpc", "http", "http/protobuf")
_consecutive_empty_polls = 0
_empty_queue_warned_at = None
# Keyed by destination name; only HTTP destinations can report partial success.
_partial_success_recorders = {}
_grpc_partial_success_warned = False
# Destination name -> whether its last export succeeded, so recovery is reported.
_destination_healthy = {}
DESTINATIONS_KEY = "otel:destinations"  # keep in sync with GlobalController.OTEL_DESTINATIONS_KEY
_redis = None


def _validate_destination(destination, index):
    if not isinstance(destination, dict):
        raise ValueError(f"destination {index} must be an object")

    name = destination.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"destination {index} name must be a non-empty string")

    protocol = destination.get("protocol")
    protocol = protocol.lower() if isinstance(protocol, str) else protocol
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"destination {name!r} protocol must be one of "
            f"{list(SUPPORTED_PROTOCOLS)}; got {protocol!r}"
        )
    endpoint = destination.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(f"destination {name!r} endpoint must be a non-empty string")

    headers = destination.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError(f"destination {name!r} headers must be an object")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError(
                f"destination {name!r} headers must map non-empty strings to strings"
            )
        headers = dict(headers)

    insecure = destination.get("insecure")
    if insecure is not None and not isinstance(insecure, bool):
        raise ValueError(f"destination {name!r} insecure must be a boolean")

    timeout = destination.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"destination {name!r} timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"destination {name!r} timeout must be a positive number")

    return {
        "name": name.strip(),
        "protocol": protocol,
        "endpoint": endpoint.strip(),
        "headers": headers,
        "insecure": insecure,
        "timeout": timeout,
    }


def _configured_destinations(raw):
    """Parse and validate the destinations JSON read from Redis."""
    if raw is None:
        return None
    try:
        destinations = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{DESTINATIONS_KEY} must contain a JSON list") from exc
    if not isinstance(destinations, list) or not destinations:
        raise ValueError(f"{DESTINATIONS_KEY} must contain a non-empty JSON list")

    validated = []
    names = set()
    for index, destination in enumerate(destinations):
        validated_destination = _validate_destination(destination, index)
        name = validated_destination["name"]
        if name in names:
            raise ValueError(f"destination names must be unique; duplicate {name!r}")
        names.add(name)
        validated.append(validated_destination)
    return validated


class _PartialSuccessRecorder:
    """Records rejected_spans from OTLP responses, which the SDK exporters discard."""

    def __init__(self, destination_name):
        self.destination_name = destination_name
        self.rejected_spans = 0
        self.error_message = ""
        self._unparseable_logged = False

    def reset(self):
        self.rejected_spans = 0
        self.error_message = ""

    def __call__(self, response, *args, **kwargs):
        if not response.ok or not response.content:
            return
        try:
            parsed = ExportTraceServiceResponse.FromString(response.content)
        except Exception as e:
            if not self._unparseable_logged:
                self._unparseable_logged = True
                logger.warning(
                    "Destination %s returned a body that is not an "
                    "ExportTraceServiceResponse, so partial rejections cannot be "
                    "detected there: %s",
                    self.destination_name,
                    e,
                )
            return
        self.rejected_spans = parsed.partial_success.rejected_spans
        self.error_message = parsed.partial_success.error_message


def _build_exporter(destination):
    """Construct one OTLP exporter, and its partial-success recorder when supported."""
    kwargs = {
        "endpoint": destination["endpoint"],
    }
    if destination["headers"] is not None: kwargs["headers"] = destination["headers"]  # fmt: skip
    if destination["timeout"] is not None: kwargs["timeout"] = destination["timeout"]  # fmt: skip

    if destination["protocol"] == "grpc":
        if destination["insecure"] is not None: kwargs["insecure"] = destination["insecure"]  # fmt: skip
        global _grpc_partial_success_warned
        if not _grpc_partial_success_warned:
            _grpc_partial_success_warned = True
            logger.warning(
                "gRPC destinations cannot report partial rejections: the SDK "
                "discards the response body, so spans this receiver rejects "
                "individually will still be marked sent."
            )
        return GrpcOTLPSpanExporter(**kwargs), None

    if destination["insecure"] is not None:
        logger.warning(
            "Destination %s specifies insecure=%s, which is ignored for HTTP exporters.",
            destination["name"],
            destination["insecure"],
        )
    recorder = _PartialSuccessRecorder(destination["name"])
    session = requests.Session()
    session.hooks["response"].append(recorder)
    kwargs["session"] = session
    return HttpOTLPSpanExporter(**kwargs), recorder


def _probe_destination(destination_name, exporter):
    """Report whether a destination actually answers, without failing startup.

    An empty export is a real OTLP request, so this exercises the endpoint,
    path, TLS and auth rather than just proving a port is open. A destination
    that is merely down yet is not fatal -- rows stay queued until it returns.
    """
    try:
        reachable = exporter.export([]) is SpanExportResult.SUCCESS
        detail = ""
    except Exception as e:
        reachable = False
        detail = f": {e}"
    if reachable:
        logger.info("OTel destination %s answered a connectivity check.", destination_name)
    else:
        logger.warning(
            "OTel destination %s did not answer a connectivity check, so nothing "
            "will reach it until that is fixed; spans stay queued meanwhile%s",
            destination_name,
            detail,
        )


def _build_metric_exporter(destination):
    """Construct one OTLP metric exporter for a destination.

    Unlike the span path there is no partial-success recorder: the OTLP metrics response
    carries its own ExportMetricsServiceResponse shape, and per-datapoint rejection
    detection is a later refinement -- for now a metrics export is accepted or retried
    whole, like a gRPC span export.
    """
    kwargs = {
        "endpoint": destination["endpoint"],
    }
    if destination["headers"] is not None: kwargs["headers"] = destination["headers"]  # fmt: skip
    if destination["timeout"] is not None: kwargs["timeout"] = destination["timeout"]  # fmt: skip

    if destination["protocol"] == "grpc":
        if destination["insecure"] is not None: kwargs["insecure"] = destination["insecure"]  # fmt: skip
        return GrpcOTLPMetricExporter(**kwargs)
    return HttpOTLPMetricExporter(**kwargs)


def _build_metric_exporters(raw):
    """Build one OTLP metric exporter per configured destination."""
    destinations = _configured_destinations(raw)
    if destinations is None:
        raise RuntimeError(f"{DESTINATIONS_KEY} is not set; otel.destinations is required")

    exporters = []
    try:
        for destination in destinations:
            exporters.append(
                (destination["name"], _build_metric_exporter(destination))
            )
    except Exception:
        _shutdown_exporters(
            exporters,
            f"discarding metric destinations already built before "
            f"{destination['name']!r} failed to build",
        )
        raise
    return exporters


def _build_trace_exporters(raw):
    """Build one OTLP span exporter per configured destination."""
    destinations = _configured_destinations(raw)
    if destinations is None:
        raise RuntimeError(f"{DESTINATIONS_KEY} is not set; otel.destinations is required")

    exporters = []
    recorders = {}
    try:
        for destination in destinations:
            exporter, recorder = _build_exporter(destination)
            exporters.append((destination["name"], exporter))
            if recorder is not None:
                recorders[destination["name"]] = recorder
            logger.info(
                "Configured OTel destination %s (%s).",
                destination["name"],
                destination["protocol"],
            )
            _probe_destination(destination["name"], exporter)
    except Exception:
        _shutdown_exporters(
            exporters,
            f"discarding destinations already built before {destination['name']!r} "
            f"failed to build",
        )
        raise
    _partial_success_recorders.clear()
    _partial_success_recorders.update(recorders)
    return exporters


def _shutdown_exporters(exporters, reason):
    """Shut down each exporter, logging rather than propagating individual failures."""
    for destination_name, exporter in exporters:
        try:
            exporter.shutdown()
        except Exception as e:
            logger.error(
                "Failed to shut down OTel destination %s while %s: %s",
                destination_name,
                reason,
                e,
                exc_info=True,
            )


def _handle_shutdown(signum, frame):
    global _running
    _running = False


def _reload_destinations_if_changed():
    # Invalid Redis values are logged and ignored -- keep the previous exporters
    # running rather than tearing down a working config over a bad update.
    global _trace_exporters, _metric_exporters, _last_destinations_raw
    try:
        raw = _redis.get(DESTINATIONS_KEY)
    except Exception as e:
        logger.error(
            "Failed to read %s from Redis; keeping the current %d destination(s): %s",
            DESTINATIONS_KEY,
            len(_trace_exporters),
            e,
            exc_info=True,
        )
        return
    if raw == _last_destinations_raw:
        return
    try:
        new_trace_exporters = _build_trace_exporters(raw)
        new_metric_exporters = _build_metric_exporters(raw)
    except Exception as e:
        logger.warning("Ignoring invalid %s update: %s", DESTINATIONS_KEY, e)
        return
    _shutdown_exporters(_trace_exporters, "replacing it after a config reload")
    _shutdown_exporters(_metric_exporters, "replacing it after a config reload")
    _trace_exporters = new_trace_exporters
    _metric_exporters = new_metric_exporters
    _last_destinations_raw = raw
    logger.info("Reloaded %d OTel destination(s) from Redis.", len(_trace_exporters))


def _read_pending_rows(query, params, source_label):
    """Read one poll's worth of pending rows for a signal.

    Shared by traces and metrics; ``source_label`` names the table/signal for the error
    messages. Never raises -- a read failure returns [] so the poll is skipped rather
    than crashing the loop.
    """
    try:
        conn = sqlite3.connect(db.DB_PATH)
    except Exception as e:
        logger.error(
            "Failed to open %s at %s; nothing exported this poll: %s",
            source_label,
            db.DB_PATH,
            e,
            exc_info=True,
        )
        return []
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    except Exception as e:
        logger.error(
            "Failed to read pending %s from %s; nothing exported this poll: %s",
            source_label,
            db.DB_PATH,
            e,
            exc_info=True,
        )
        return []
    finally:
        conn.close()


def _deliver_and_mark(exporters, deliver, ids, mark_fn, unit):
    """Fan a converted batch out to every destination, then mark it sent only if all
    accepted. Shared by traces and metrics.

    ``deliver(destination_name, exporter) -> bool`` performs one destination's export and
    its result check (this is where the signals differ -- SpanExportResult plus the
    partial-success recorder for traces, MetricExportResult for metrics). ``mark_fn(ids,
    db_path)`` acknowledges the batch. Tracks per-destination health, and on any failure
    leaves every row unsent for the next poll to retry (deterministic ids collapse the
    duplicates at the backend).
    """
    failed_destinations = []
    for destination_name, exporter in exporters:
        delivered = deliver(destination_name, exporter)
        if not delivered:
            failed_destinations.append(destination_name)
        elif _destination_healthy.get(destination_name) is False:
            logger.info(
                "OTel destination %s is accepting telemetry again.", destination_name
            )
        _destination_healthy[destination_name] = delivered

    if failed_destinations:
        logger.warning(
            "Leaving %d %s unsent for retry; failed destination(s): %s",
            len(ids),
            unit,
            ", ".join(failed_destinations),
        )
        return

    try:
        mark_fn(ids, db.DB_PATH)
    except Exception as e:
        logger.error(
            "Exported %d %s but failed to mark them sent in %s -- they will be "
            "re-exported and duplicated on the next poll: %s",
            len(ids),
            unit,
            db.DB_PATH,
            e,
            exc_info=True,
        )
        return
    logger.info("Exported %d %s to all configured OTel destinations.", len(ids), unit)


def _reject_unexportable(span):
    """Raise if the OTLP encoder cannot serialize this span.

    Encoding is what actually rejects a bad row (an out-of-range id, an
    unencodable attribute), and it happens inside the batched export() call --
    where one row's failure discards every other span in the batch. Doing it per
    row here keeps a single bad future from blocking everything behind it.
    """
    encode_spans([span]).SerializePartialToString()


def _placeholder_after_repeated_failure(row, error):
    """Return a placeholder span once a row has failed too often, else None.

    Only per-row conversion/encoding failures count here. An export failure is
    shared by the whole batch, so counting those would replace every row in the
    queue with a placeholder after a spell of receiver downtime.
    """
    future_id = row["future_id"]
    attempts = _row_export_failures.get(future_id, 0) + 1
    _row_export_failures[future_id] = attempts
    if attempts < MAX_ROW_EXPORT_ATTEMPTS:
        logger.error(
            "Skipping waiting row %s -- cannot be exported (attempt %d of %d): %s",
            future_id,
            attempts,
            MAX_ROW_EXPORT_ATTEMPTS,
            error,
        )
        return None
    try:
        placeholder = convert.invalid_row_placeholder_span(row, str(error))
        _reject_unexportable(placeholder)
    except Exception as e:
        logger.error(
            "Waiting row %s cannot be exported and no placeholder could be built "
            "for it either, so it stays in the queue: %s",
            future_id,
            e,
            exc_info=True,
        )
        return None
    logger.warning(
        "Waiting row %s failed %d export attempts; sending a placeholder span in "
        "its place so the future is not lost silently: %s",
        future_id,
        attempts,
        error,
    )
    _row_export_failures.pop(future_id, None)
    return placeholder


def _waiting_row_count():
    """Total rows in waiting, or None when the table cannot be counted."""
    try:
        conn = sqlite3.connect(db.DB_PATH)
        try:
            return conn.execute("SELECT COUNT(*) FROM waiting").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        logger.error(
            "Failed to count rows in %s: %s", db.DB_PATH, e, exc_info=True
        )
        return None


def _note_queue_state(found_pending):
    """Warn once if no row ever appears, which sqlite cannot report as an error.

    A missing database file is created rather than refused, so a misdirected
    DB_PATH looks exactly like an idle queue until someone compares the two paths.
    """
    global _consecutive_empty_polls, _empty_queue_warned_at
    if found_pending:
        _consecutive_empty_polls = 0
        return
    _consecutive_empty_polls += 1
    if _consecutive_empty_polls < EMPTY_QUEUE_WARNING_POLLS:
        return
    if (
        _empty_queue_warned_at is not None
        and _consecutive_empty_polls - _empty_queue_warned_at < EMPTY_QUEUE_REWARN_POLLS
    ):
        return
    # Only latch once the condition is confirmed, so a failed count re-checks
    # next poll instead of silencing the warning for the life of the process.
    if _waiting_row_count() != 0:
        return
    _empty_queue_warned_at = _consecutive_empty_polls
    logger.warning(
        "No rows have ever appeared in %s after %d consecutive polls. Spans are "
        "only exported from this file, so GlobalController may be writing futures "
        "to a different otel_queue.db than this process is reading.",
        db.DB_PATH,
        _consecutive_empty_polls,
    )


def _send_pending_traces():
    """Export finished, not-yet-sent waiting rows and mark them only once delivered."""
    exporters = _trace_exporters
    if not exporters:
        raise RuntimeError("OTel trace exporter has no configured destinations")

    rows = _read_pending_rows(
        "SELECT * FROM waiting WHERE finished_at IS NOT NULL "
        "AND (sent IS NULL OR sent = 0) LIMIT ?",
        (MAX_SPANS_PER_POLL,),
        "the waiting database",
    )
    _note_queue_state(bool(rows))
    if not rows:
        return

    spans = []
    future_ids = []
    for row in rows:
        try:
            span = convert.waiting_row_to_span(row)
            _reject_unexportable(span)
        except Exception as e:
            span = _placeholder_after_repeated_failure(row, e)
            if span is None:
                continue
        else:
            _row_export_failures.pop(row["future_id"], None)
        spans.append(span)
        future_ids.append(row["future_id"])
    if not spans:
        return

    def deliver(destination_name, exporter):
        # Span-specific delivery: SpanExportResult plus the HTTP partial-success recorder.
        recorder = _partial_success_recorders.get(destination_name)
        if recorder is not None:
            recorder.reset()
        try:
            result = exporter.export(spans)
        except Exception as e:
            logger.error(
                "Destination %s raised while exporting %d span(s): %s",
                destination_name,
                len(spans),
                e,
            )
            return False
        if result is not SpanExportResult.SUCCESS:
            logger.error(
                "Destination %s failed to export %d span(s).",
                destination_name,
                len(spans),
            )
            return False
        if recorder is not None and recorder.rejected_spans:
            logger.error(
                "Destination %s accepted the request but rejected %d of %d span(s), "
                "so the batch is not acknowledged: %s",
                destination_name,
                recorder.rejected_spans,
                len(spans),
                recorder.error_message or "no reason given",
            )
            return False
        return True

    _deliver_and_mark(exporters, deliver, future_ids, db.mark_sent_many, "span(s)")


def _send_pending_metrics():
    """Export not-yet-sent machine metrics samples, marking them only once delivered.

    Mirrors _send_pending_traces via the shared _read_pending_rows / _deliver_and_mark
    helpers: read a bounded batch, convert each row to a ResourceMetrics, export the whole
    batch to every destination as one MetricsData, and mark the samples sent only when all
    destinations accept. There is no per-row placeholder like the span path -- a sample
    that fails to convert is dropped (logged), since a partial machine sample has no
    useful stand-in.
    """
    exporters = _metric_exporters
    if not exporters:
        raise RuntimeError("OTel metric exporter has no configured destinations")

    rows = _read_pending_rows(
        "SELECT * FROM metrics_waiting WHERE sent IS NULL OR sent = 0 LIMIT ?",
        (MAX_SPANS_PER_POLL,),
        "the metrics database",
    )
    if not rows:
        return

    resource_metrics = []
    sample_ids = []
    for row in rows:
        try:
            converted = metric_convert.metrics_row_to_resource_metrics(row)
        except Exception as e:
            logger.error(
                "Skipping metrics sample %s -- failed to convert: %s",
                row["sample_id"],
                e,
            )
            continue
        if converted is None:
            # Unparseable/empty sample: mark sent so it isn't retried forever.
            try:
                db.mark_metrics_sent(row["sample_id"], db.DB_PATH)
            except Exception as e:
                logger.error(
                    "Failed to mark unconvertible metrics sample %s done: %s",
                    row["sample_id"],
                    e,
                )
            continue
        resource_metrics.append(converted)
        sample_ids.append(row["sample_id"])
    if not resource_metrics:
        return

    metrics_data = MetricsData(resource_metrics=resource_metrics)

    def deliver(destination_name, exporter):
        # Metric-specific delivery: whole-batch MetricExportResult, no partial-success
        # recorder (per-datapoint rejection detection is a later refinement).
        try:
            result = exporter.export(metrics_data)
        except Exception as e:
            logger.error(
                "Destination %s raised while exporting %d metric sample(s): %s",
                destination_name,
                len(sample_ids),
                e,
            )
            return False
        if result is not MetricExportResult.SUCCESS:
            logger.error(
                "Destination %s failed to export %d metric sample(s).",
                destination_name,
                len(sample_ids),
            )
            return False
        return True

    _deliver_and_mark(
        exporters, deliver, sample_ids, db.mark_metrics_sent_many, "metric sample(s)"
    )


def main():
    global _trace_exporters, _metric_exporters, _redis, _last_destinations_raw
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    try:
        db.init_db()
    except Exception as e:
        logger.error(
            "Fatal: cannot initialize the waiting database at %s: %s",
            db.DB_PATH,
            e,
            exc_info=True,
        )
        raise
    try:
        _redis = RedisClient(host="host.docker.internal")
        _last_destinations_raw = _redis.get(DESTINATIONS_KEY)
    except Exception as e:
        logger.error(
            "Fatal: cannot reach Redis to read %s: %s", DESTINATIONS_KEY, e, exc_info=True
        )
        raise
    try:
        _trace_exporters = _build_trace_exporters(_last_destinations_raw)
        _metric_exporters = _build_metric_exporters(_last_destinations_raw)
    except Exception as e:
        logger.error(
            "Fatal: cannot build OTel destinations from %s: %s",
            DESTINATIONS_KEY,
            e,
            exc_info=True,
        )
        raise
    logger.info(
        "OTel exporter process started with %d destination(s).", len(_trace_exporters)
    )
    try:
        last_poll = 0
        while _running:
            if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
                try:
                    _reload_destinations_if_changed()
                    _send_pending_traces()
                    _send_pending_metrics()
                except Exception as e:
                    logger.error(
                        "Unexpected error in OTel export poll cycle (non-fatal, "
                        "retrying next tick): %s",
                        e,
                        exc_info=True,
                    )
                last_poll = time.time()
            time.sleep(1)
    finally:
        _shutdown_exporters(_trace_exporters, "shutting the exporter process down")
        _shutdown_exporters(_metric_exporters, "shutting the exporter process down")
        logger.info("OTel exporter process exiting.")


if __name__ == "__main__":
    main()
