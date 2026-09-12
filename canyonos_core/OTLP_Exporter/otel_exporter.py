"""Entrypoint for the OTLP exporter process.

Each poll tick reads not-yet-sent rows from SQLite, converts each to the OTel object for
its signal, hands it to every configured destination, and marks it sent only after all
destinations accept it. Batching, OTLP serialization, and sending remain the SDK's
responsibility (see DESIGN.md).

Signals are handled uniformly through ``_process_signal``: traces (``waiting`` table) and
machine metrics (``metrics_waiting`` table) today, with logs slotting in the same way once
feature/error_polling lands. Traces/logs use the SDK's Batch*Processor push model; metrics
have no such processor, so they use the OTLP MetricExporter's direct ``export`` call --
otherwise the poll/convert/fan-out/mark-sent flow is identical across signals.

Destinations come from the ``otel:destinations`` Redis key (GlobalController writes it),
not env -- every poll tick re-reads it and rebuilds exporters if it changed, so a config
reload (SIGHUP) reaches this process without a restart.
"""

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
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcOTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpOTLPMetricExporter,
)
from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import convert
import metric_convert
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_running = True
_trace_processors = []  # (name, BatchSpanProcessor)
_metric_exporters = []  # (name, OTLPMetricExporter)
_last_destinations_raw = None
POLL_INTERVAL_SECONDS = 5
DESTINATIONS_KEY = "otel:destinations"  # keep in sync with GlobalController.OTEL_DESTINATIONS_KEY
_redis = None


def _validate_destination(destination, index):
    if not isinstance(destination, dict):
        raise ValueError(f"destination {index} must be an object")

    name = destination.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"destination {index} name must be a non-empty string")

    protocol = destination.get("protocol")  # must be exactly "grpc" or "http"
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


def _build_exporter(destination, signal):
    """Construct one OTLP exporter for the given signal ('traces' or 'metrics')."""
    kwargs = {
        "endpoint": destination["endpoint"],
    }
    if destination["headers"] is not None: kwargs["headers"] = destination["headers"]  # fmt: skip
    if destination["timeout"] is not None: kwargs["timeout"] = destination["timeout"]  # fmt: skip

    if destination["protocol"] == "grpc":
        if destination["insecure"] is not None: kwargs["insecure"] = destination["insecure"]  # fmt: skip
        return (
            GrpcOTLPSpanExporter(**kwargs)
            if signal == "traces"
            else GrpcOTLPMetricExporter(**kwargs)
        )

    if destination["insecure"] is not None:
        logger.warning(
            "Destination %s specifies insecure=%s, which is ignored for HTTP exporters.",
            destination["name"],
            destination["insecure"],
        )
    return (
        HttpOTLPSpanExporter(**kwargs)
        if signal == "traces"
        else HttpOTLPMetricExporter(**kwargs)
    )


def _build_trace_processors(destinations):
    """Build one BatchSpanProcessor per configured destination."""
    trace_processors = []
    try:
        for destination in destinations:
            trace_processors.append((
                destination["name"],
                BatchSpanProcessor(
                    _build_exporter(destination, "traces"), schedule_delay_millis=1000
                ),
            ))
    except Exception:
        for _, processor in trace_processors:
            processor.shutdown()
        raise
    return trace_processors


def _build_metric_exporters(destinations):
    """Build one OTLP metric exporter per configured destination."""
    metric_exporters = []
    try:
        for destination in destinations:
            metric_exporters.append(
                (destination["name"], _build_exporter(destination, "metrics"))
            )
    except Exception:
        for _, exporter in metric_exporters:
            exporter.shutdown()
        raise
    return metric_exporters


def _build_pipelines(raw):
    """Build the trace + metric pipeline for every configured destination.

    Returns ``(trace_processors, metric_exporters)``. If the metric pipeline fails to
    build after the trace one already succeeded, the trace processors are torn down too,
    so a partial build never leaks live exporters.
    """
    destinations = _configured_destinations(raw)
    if destinations is None:
        raise RuntimeError(f"{DESTINATIONS_KEY} is not set; otel.destinations is required")

    trace_processors = _build_trace_processors(destinations)
    try:
        metric_exporters = _build_metric_exporters(destinations)
    except Exception:
        for _, processor in trace_processors:
            processor.shutdown()
        raise

    for destination in destinations:
        logger.info(
            "Configured OTel destination %s (%s).",
            destination["name"],
            destination["protocol"],
        )
    return trace_processors, metric_exporters


def _handle_shutdown(signum, frame):
    global _running
    _running = False


def _reload_destinations_if_changed():
    # Invalid Redis values are logged and ignored -- keep the previous exporters
    # running rather than tearing down a working config over a bad update.
    global _trace_processors, _metric_exporters, _last_destinations_raw
    raw = _redis.get(DESTINATIONS_KEY)
    if raw == _last_destinations_raw:
        return
    try:
        new_trace_processors, new_metric_exporters = _build_pipelines(raw)
    except Exception as e:
        logger.warning("Ignoring invalid %s update: %s", DESTINATIONS_KEY, e)
        return
    for _, processor in _trace_processors:
        processor.shutdown()
    for _, exporter in _metric_exporters:
        exporter.shutdown()
    _trace_processors = new_trace_processors
    _metric_exporters = new_metric_exporters
    _last_destinations_raw = raw
    logger.info("Reloaded %d OTel destination(s) from Redis.", len(_trace_processors))


def _process_signal(query, convert_fn, destinations, emit_fn, mark_fn, signal_name,
                    key_column="future_id"):
    """Generic send loop shared by every signal.

    Polls ``query`` rows from SQLite, converts each with ``convert_fn`` (which must return
    a list of OTel items), fans each item out to every ``destinations`` entry via
    ``emit_fn``, and calls ``mark_fn(row[key_column])`` only after all destinations accept.
    ``key_column`` is the row's primary key -- ``future_id`` for trace/log rows in
    ``waiting``, ``sample_id`` for metric rows in ``metrics_waiting``.
    """
    if not destinations:
        raise RuntimeError(f"OTel {signal_name} exporter has no configured destinations")

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    if not rows:
        return

    sent_count = 0
    for row in rows:
        key = row[key_column]
        try:
            items = convert_fn(row)
        except Exception as e:
            logger.error(
                "Skipping %s row %s -- failed to convert: %s", signal_name, key, e
            )
            continue

        if not items:
            # Conversion produced nothing (e.g. an unparseable row) -- mark done so it
            # isn't retried forever.
            mark_fn(key)
            continue

        failed_destinations = []
        for destination_name, destination in destinations:
            try:
                for item in items:
                    emit_fn(destination, item)
            except Exception as e:
                # Still offer to the remaining destinations. The row is only acknowledged
                # once every destination accepted it, so a failure is retried next poll.
                failed_destinations.append(destination_name)
                logger.error(
                    "Destination %s rejected %s row %s: %s",
                    destination_name, signal_name, key, e,
                )
        if failed_destinations:
            continue
        mark_fn(key)
        sent_count += 1
    logger.info(
        "Queued %d %s row(s) for all configured OTel destinations.",
        sent_count, signal_name,
    )


def _send_pending_traces():
    """Convert and send each finished, not-yet-sent trace span."""
    _process_signal(
        query=(
            "SELECT * FROM waiting WHERE finished_at IS NOT NULL "
            "AND (sent IS NULL OR sent = 0)"
        ),
        convert_fn=lambda row: [convert.waiting_row_to_span(row)],
        destinations=_trace_processors,
        emit_fn=lambda processor, span: processor.on_end(span),
        mark_fn=db.mark_sent,
        signal_name="trace",
    )


def _export_metrics_or_raise(exporter, metrics_data):
    """Export via the OTLP MetricExporter, raising on non-success so the generic loop's
    fan-out failure handling (and retry-next-poll) works the same as for traces."""
    result = exporter.export(metrics_data)
    if result != MetricExportResult.SUCCESS:
        raise RuntimeError(f"metric export returned {result}")


def _send_pending_metrics():
    """Convert and send each not-yet-sent machine metrics sample."""
    _process_signal(
        query="SELECT * FROM metrics_waiting WHERE sent IS NULL OR sent = 0",
        convert_fn=metric_convert.metrics_row_to_metrics_data,
        destinations=_metric_exporters,
        emit_fn=_export_metrics_or_raise,
        mark_fn=db.mark_metrics_sent,
        signal_name="metric",
        key_column="sample_id",
    )


def main():
    global _trace_processors, _metric_exporters, _redis, _last_destinations_raw
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    db.init_db()
    # GC reaches its own Redis via host.docker.internal (a sibling container,
    # not the same network namespace, since GC runs on bridge networking) --
    # match that instead of plain localhost.
    _redis = RedisClient(host="host.docker.internal")
    _last_destinations_raw = _redis.get(DESTINATIONS_KEY)
    _trace_processors, _metric_exporters = _build_pipelines(_last_destinations_raw)
    logger.info("OTel exporter process started with %d destination(s).", len(_trace_processors))
    try:
        last_poll = 0
        while _running:
            if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
                try:
                    _reload_destinations_if_changed()
                except Exception as e:
                    logger.warning("Destination reload failed (non-fatal): %s", e)
                try:
                    _send_pending_traces()
                except Exception as e:
                    logger.warning("Trace poll cycle failed (non-fatal): %s", e)
                try:
                    _send_pending_metrics()
                except Exception as e:
                    logger.warning("Metric poll cycle failed (non-fatal): %s", e)
                last_poll = time.time()
            time.sleep(1)
    finally:
        for destination_name, processor in _trace_processors:
            try:
                processor.shutdown()
            except Exception as e:
                logger.error(
                    "Failed to shut down OTel destination %s: %s", destination_name, e
                )
        for destination_name, exporter in _metric_exporters:
            try:
                exporter.shutdown()
            except Exception as e:
                logger.error(
                    "Failed to shut down OTel metric destination %s: %s",
                    destination_name, e,
                )
        logger.info("OTel exporter process exiting.")


if __name__ == "__main__":
    main()
