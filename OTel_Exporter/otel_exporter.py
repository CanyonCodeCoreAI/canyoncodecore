"""Entrypoint for the OTLP exporter process.

Each poll tick reads finished, not-yet-sent rows from ``waiting``, converts each to a
span, hands it to every configured BatchSpanProcessor, and marks it sent only after
all processors accept it. Batching, OTLP serialization, and sending remain the SDK's
responsibility (see DESIGN.md).

GlobalController may provide a JSON list in ``VENTIS_OTEL_DESTINATIONS``. That is a
Ventis-specific configuration because the standard OTEL exporter environment
variables describe only one destination. If it is absent, the original single
destination behavior is retained: the exporter class and its settings are selected
from the standard OTEL environment variables and SDK defaults.
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
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import convert
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_running = True
_processor = None
_processors = []
POLL_INTERVAL_SECONDS = 5
DESTINATIONS_ENV = "VENTIS_OTEL_DESTINATIONS"


def _normalize_protocol(protocol):
    """Return the exporter family for a configured protocol name."""
    if not isinstance(protocol, str) or not protocol.strip():
        raise ValueError("destination protocol must be a non-empty string")
    normalized = protocol.strip().lower().replace("_", "-")
    if normalized in {"grpc", "otlp/grpc", "grpc/protobuf", "grpc-protobuf"}:
        return "grpc"
    if normalized in {
        "http",
        "http/protobuf",
        "http-protobuf",
        "http/proto",
        "http+protobuf",
        "protobuf",
    }:
        return "http"
    raise ValueError(
        f"unsupported destination protocol {protocol!r}; expected grpc or http/protobuf"
    )


def _validate_destination(destination, index):
    if not isinstance(destination, dict):
        raise ValueError(f"destination {index} must be an object")

    name = destination.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"destination {index} name must be a non-empty string")

    protocol = _normalize_protocol(destination.get("protocol"))
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
    """Parse and validate the Ventis multi-destination environment variable.

    ``None`` means no Ventis-specific configuration was supplied, so callers can
    preserve legacy OTEL environment-variable behavior. An empty or malformed value
    is an explicit configuration error and fails startup rather than silently
    exporting to the wrong destination.
    """
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


def _build_exporter(destination):
    """Construct one explicitly configured exporter without logging credentials."""
    kwargs = {
        "endpoint": destination["endpoint"],
    }
    if destination["headers"] is not None:
        kwargs["headers"] = destination["headers"]
    if destination["timeout"] is not None:
        kwargs["timeout"] = destination["timeout"]

    if destination["protocol"] == "grpc":
        if destination["insecure"] is not None:
            kwargs["insecure"] = destination["insecure"]
        return GrpcOTLPSpanExporter(**kwargs)

    if destination["insecure"] is not None:
        logger.warning(
            "Destination %s specifies insecure=%s, which is ignored for HTTP exporters.",
            destination["name"],
            destination["insecure"],
        )
    return HttpOTLPSpanExporter(**kwargs)


def _build_processors():
    """Build destination processors, or one legacy processor when unconfigured."""
    destinations = _configured_destinations()
    if destinations is None:
        protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").lower()
        exporter_class = (
            HttpOTLPSpanExporter if protocol.startswith("http") else GrpcOTLPSpanExporter
        )
        return [("legacy", BatchSpanProcessor(exporter_class(), schedule_delay_millis=1000))]

    processors = []
    try:
        for destination in destinations:
            exporter = _build_exporter(destination)
            processors.append(
                (
                    destination["name"],
                    BatchSpanProcessor(exporter, schedule_delay_millis=1000),
                )
            )
            logger.info(
                "Configured OTel destination %s (%s).",
                destination["name"],
                destination["protocol"],
            )
    except Exception:
        for _, processor in processors:
            processor.shutdown()
        raise
    return processors


def _handle_shutdown(signum, frame):
    global _running
    _running = False


def _send_pending():
    """Convert and send each finished, not-yet-sent waiting row."""
    processors = _processors
    if not processors and _processor is not None:
        # Compatibility for callers that configured the pre-fan-out singular
        # ``_processor`` directly (the normal startup path always populates both).
        processors = [("legacy", _processor)]
    if not processors:
        raise RuntimeError("OTel exporter has no configured processors")

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM waiting WHERE finished_at IS NOT NULL "
            "AND (sent IS NULL OR sent = 0)"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    sent_count = 0
    for row in rows:
        try:
            span = convert.waiting_row_to_span(row)
        except Exception as e:
            logger.error(
                "Skipping waiting row %s -- failed to convert: %s", row["future_id"], e
            )
            continue

        failed_destinations = []
        for destination_name, processor in processors:
            try:
                processor.on_end(span)
            except Exception as e:
                # Still offer the span to the remaining processors. The row is only
                # acknowledged when every destination accepted it, so a failed
                # destination will be retried by the next poll.
                failed_destinations.append(destination_name)
                logger.error(
                    "Destination %s rejected waiting row %s: %s",
                    destination_name,
                    row["future_id"],
                    e,
                )
        if failed_destinations:
            continue
        db.mark_sent(row["future_id"])
        sent_count += 1
    logger.info("Queued %d span(s) for all configured OTel destinations.", sent_count)


def main():
    global _processor, _processors
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    db.init_db()
    _processors = _build_processors()
    # Keep the old singular module variable available to integrations that imported
    # it, while all sending uses the destination-aware collection above.
    _processor = _processors[0][1]
    logger.info("OTel exporter process started with %d destination(s).", len(_processors))
    try:
        last_poll = 0
        while _running:
            if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
                try:
                    _send_pending()
                except Exception as e:
                    logger.warning("Poll cycle failed (non-fatal): %s", e)
                last_poll = time.time()
            time.sleep(1)
    finally:
        for destination_name, processor in _processors:
            try:
                processor.shutdown()
            except Exception as e:
                logger.error(
                    "Failed to shut down OTel destination %s: %s", destination_name, e
                )
        logger.info("OTel exporter process exiting.")


if __name__ == "__main__":
    main()
