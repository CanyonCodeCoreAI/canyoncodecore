"""Entrypoint for the OTLP exporter process.

Each poll tick reads finished, not-yet-sent rows from ``waiting``, converts each to a
span, hands it to every configured BatchSpanProcessor, and marks it sent only after
all processors accept it. Batching, OTLP serialization, and sending remain the SDK's
responsibility (see DESIGN.md).

GlobalController provides a JSON list in ``VENTIS_OTEL_DESTINATIONS``, required because
the standard OTEL exporter environment variables describe only one destination.
"""

import json
import logging
import math
import os
import signal
import sqlite3
import time

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpOTLPSpanExporter,
)
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter as GrpcOTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.http._log_exporter import (
    OTLPLogExporter as HttpOTLPLogExporter,
)
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import span_convert
import log_convert
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_running = True
_processors = []      # (name, BatchSpanProcessor)
_log_processors = []  # (name, BatchLogRecordProcessor)
POLL_INTERVAL_SECONDS = 5
DESTINATIONS_ENV = "VENTIS_OTEL_DESTINATIONS"


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


def _configured_destinations():
    """Parse and validate the Ventis multi-destination environment variable."""
    raw = os.environ.get(DESTINATIONS_ENV)
    if raw is None:
        return None
    try:
        destinations = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{DESTINATIONS_ENV} must contain a JSON list") from exc
    if not isinstance(destinations, list) or not destinations:
        raise ValueError(f"{DESTINATIONS_ENV} must contain a non-empty JSON list")

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
    """Construct one OTLP exporter for the given signal ('traces' or 'logs')."""
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
            else GrpcOTLPLogExporter(**kwargs)
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
        else HttpOTLPLogExporter(**kwargs)
    )


def _build_processors():
    """Build one span + one log processor pair per configured destination."""
    destinations = _configured_destinations()
    if destinations is None:
        raise RuntimeError(f"{DESTINATIONS_ENV} is not set; otel.destinations is required")

    span_processors = []
    log_processors = []
    try:
        for destination in destinations:
            span_processors.append((
                destination["name"],
                BatchSpanProcessor(
                    _build_exporter(destination, "traces"),
                    schedule_delay_millis=1000,
                ),
            ))
            log_processors.append((
                destination["name"],
                BatchLogRecordProcessor(
                    _build_exporter(destination, "logs"),
                    schedule_delay_millis=1000,
                ),
            ))
            logger.info(
                "Configured OTel destination %s (%s).",
                destination["name"],
                destination["protocol"],
            )
    except Exception:
        for _, p in span_processors + log_processors:
            p.shutdown()
        raise
    return span_processors, log_processors


def _handle_shutdown(signum, frame):
    global _running
    _running = False


def _process_signal(query, convert_fn, processors, emit_fn, mark_fn, signal_name):
    """Generic send loop shared by span and log signals.

    Polls ``query`` rows from SQLite, converts each with ``convert_fn``
    (which must return a list), fans out to every destination via ``emit_fn``,
    and calls ``mark_fn`` only after all destinations accept.
    """
    if not processors:
        raise RuntimeError(f"OTel {signal_name} exporter has no configured processors")

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
        try:
            items = convert_fn(row)
        except Exception as e:
            logger.error(
                "Skipping %s row %s -- failed to convert: %s",
                signal_name, row["future_id"], e,
            )
            continue

        if not items:
            # Conversion produced nothing (e.g. empty logs list) -- mark done.
            mark_fn(row["future_id"])
            continue

        failed_destinations = []
        for destination_name, processor in processors:
            try:
                for item in items:
                    emit_fn(processor, item)
            except Exception as e:
                failed_destinations.append(destination_name)
                logger.error(
                    "Destination %s rejected %s row %s: %s",
                    destination_name, signal_name, row["future_id"], e,
                )
        if failed_destinations:
            continue
        mark_fn(row["future_id"])
        sent_count += 1
    logger.info(
        "Queued %d %s row(s) for all configured OTel destinations.",
        sent_count, signal_name,
    )


def _send_pending():
    """Send finished, not-yet-sent spans."""
    _process_signal(
        query=(
            "SELECT * FROM waiting WHERE finished_at IS NOT NULL "
            "AND (sent IS NULL OR sent = 0)"
        ),
        convert_fn=lambda row: [span_convert.waiting_row_to_span(row)],
        processors=_processors,
        emit_fn=lambda proc, item: proc.on_end(item),
        mark_fn=db.mark_sent,
        signal_name="span",
    )


def _send_pending_logs():
    """Send logs for finished rows not yet exported."""
    _process_signal(
        query=(
            "SELECT * FROM waiting "
            "WHERE logs IS NOT NULL AND finished_at IS NOT NULL "
            "AND (logs_sent IS NULL OR logs_sent = 0)"
        ),
        convert_fn=log_convert.waiting_row_to_log_records,
        processors=_log_processors,
        emit_fn=lambda proc, item: proc.emit(item),
        mark_fn=db.mark_logs_sent,
        signal_name="log",
    )


def main():
    global _processors, _log_processors
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    db.init_db()
    _processors, _log_processors = _build_processors()
    logger.info(
        "OTel exporter process started with %d destination(s).", len(_processors)
    )
    try:
        last_poll = 0
        while _running:
            if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
                try:
                    _send_pending()
                except Exception as e:
                    logger.warning("Span poll cycle failed (non-fatal): %s", e)
                try:
                    _send_pending_logs()
                except Exception as e:
                    logger.warning("Log poll cycle failed (non-fatal): %s", e)
                last_poll = time.time()
            time.sleep(1)
    finally:
        for destination_name, processor in _processors + _log_processors:
            try:
                processor.shutdown()
            except Exception as e:
                logger.error(
                    "Failed to shut down OTel destination %s: %s", destination_name, e
                )
        logger.info("OTel exporter process exiting.")


if __name__ == "__main__":
    main()
