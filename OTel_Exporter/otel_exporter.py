"""Entrypoint for the OTLP Exporter process.

Each poll tick: read finished, not-yet-sent rows from `waiting`, convert each to a span,
hand it to a BatchSpanProcessor/OTLPSpanExporter, and mark it sent -- batching, OTLP
serialization, and sending are all the SDK's own code, not ours (see DESIGN.md). Each
row's send-and-mark-sent is atomic and happens immediately after its own successful
send, not batched at the end, so a crash mid-poll can't leave an already-sent row
unmarked (which would cause a duplicate send next run). `OTLPSpanExporter()` takes no
explicit endpoint/headers here -- it falls back to the SDK's own standard
`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` env vars, or localhost:4317,
per the SDK's own default behavior. GlobalController sets those env vars (plus
`OTEL_EXPORTER_OTLP_PROTOCOL`, which this module reads itself below to pick the gRPC vs
HTTP class) from `global_controller.yaml`'s `otel:` section when it spawns this process;
this file has no YAML/app-config awareness of its own, only standard OTel env vars --
see DESIGN.md.
"""

import logging
import os
import signal
import sqlite3
import time

# Protocol is the one thing the SDK's own exporter classes don't self-select from
# OTEL_EXPORTER_OTLP_PROTOCOL -- endpoint/headers/auth stay fully env-var-driven via
# each class's own defaults; see OTel_Exporter/DESIGN.md.
if os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").startswith("http"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
else:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import convert
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_running = True
_processor = None
POLL_INTERVAL_SECONDS = 5


def _handle_shutdown(signum, frame):
    global _running
    _running = False


def _send_pending():
    """Convert and send each finished, not-yet-sent waiting row."""
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
            _processor.on_end(span)
        except Exception as e:
            logger.error(
                "Skipping waiting row %s -- failed to send: %s", row["future_id"], e
            )
            continue
        db.mark_sent(row["future_id"])
        sent_count += 1
    logger.info("Sent %d span(s) to the batch processor.", sent_count)


def main():
    global _processor
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    db.init_db()
    _processor = BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000)
    logger.info("OTel exporter process started.")
    last_poll = 0
    while _running:
        if time.time() - last_poll >= POLL_INTERVAL_SECONDS:
            try:
                _send_pending()
            except Exception as e:
                logger.warning("Poll cycle failed (non-fatal): %s", e)
            last_poll = time.time()
        time.sleep(1)
    _processor.shutdown()
    logger.info("OTel exporter process exiting.")


if __name__ == "__main__":
    main()
