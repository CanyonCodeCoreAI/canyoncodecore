"""Entrypoint for the OTLP exporter process.

Each poll tick reads finished, not-yet-sent rows from ``waiting``, converts each to a
span, hands it to every configured BatchSpanProcessor, and marks it sent only after
all processors accept it. Batching, OTLP serialization, and sending remain the SDK's
responsibility (see DESIGN.md).

Destinations come from the ``otel:destinations`` Redis key (GlobalController writes it),
not env -- every poll tick re-reads it and rebuilds processors if it changed, so a config
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
from ventis.controller.utils.redis_client import RedisClient

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
_processors = []
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


def _build_exporter(destination):
    """Construct one OTLP exporter."""
    kwargs = {
        "endpoint": destination["endpoint"],
    }
    if destination["headers"] is not None: kwargs["headers"] = destination["headers"]  # fmt: skip
    if destination["timeout"] is not None: kwargs["timeout"] = destination["timeout"]  # fmt: skip

    if destination["protocol"] == "grpc":
        if destination["insecure"] is not None: kwargs["insecure"] = destination["insecure"]  # fmt: skip
        return GrpcOTLPSpanExporter(**kwargs)

    if destination["insecure"] is not None:
        logger.warning(
            "Destination %s specifies insecure=%s, which is ignored for HTTP exporters.",
            destination["name"],
            destination["insecure"],
        )
    return HttpOTLPSpanExporter(**kwargs)


def _build_processors(raw):
    """Build one exporter/BatchSpanProcessor pair per configured destination."""
    destinations = _configured_destinations(raw)
    if destinations is None:
        raise RuntimeError(f"{DESTINATIONS_KEY} is not set; otel.destinations is required")

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


def _reload_destinations_if_changed():
    # Invalid Redis values are logged and ignored -- keep the previous processors
    # running rather than tearing down a working config over a bad update.
    global _processors, _last_destinations_raw
    raw = _redis.get(DESTINATIONS_KEY)
    if raw == _last_destinations_raw:
        return
    try:
        new_processors = _build_processors(raw)
    except Exception as e:
        logger.warning("Ignoring invalid %s update: %s", DESTINATIONS_KEY, e)
        return
    for _, processor in _processors:
        processor.shutdown()
    _processors = new_processors
    _last_destinations_raw = raw
    logger.info("Reloaded %d OTel destination(s) from Redis.", len(_processors))


def _send_pending():
    """Convert and send each finished, not-yet-sent waiting row."""
    processors = _processors
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
    global _processors, _redis, _last_destinations_raw
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    db.init_db()
    # GC reaches its own Redis via host.docker.internal (a sibling container,
    # not the same network namespace, since GC runs on bridge networking) --
    # match that instead of plain localhost.
    _redis = RedisClient(host="host.docker.internal")
    _last_destinations_raw = _redis.get(DESTINATIONS_KEY)
    _processors = _build_processors(_last_destinations_raw)
    logger.info("OTel exporter process started with %d destination(s).", len(_processors))
    try:
        last_poll = 0
        while _running:
            if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
                try:
                    _reload_destinations_if_changed()
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
