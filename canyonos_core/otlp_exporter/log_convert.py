"""Converts a ``logs_waiting`` row into OTel ReadableLogRecord objects.

Each row is one log record exploded from a future's `logs` JSON array by
canyonos_core.controller.utils.otel_writer.log_write_rows, originally built by
canyonos_core.controller.utils.log_entry (OTel Log Data Model shape). Trace attribution
reuses the same future=span mapping from trace_convert: session_id→trace_id (128-bit),
future_id→span_id (64-bit lossy truncation).

Pure function, no I/O.
"""

import json

from opentelemetry._logs import LogRecord
from opentelemetry._logs.severity import SeverityNumber
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.resources import Resource

from utils.otlp_utils import _SAMPLED, to_epoch_nanos, trace_id_from_session, span_id_from_future

# Python stdlib levelname → OTel SeverityText closed vocabulary.
# logging.WARNING → "WARNING"; logging.CRITICAL → "CRITICAL" — remap at export time
# per the OTel spec's 6-name vocabulary (TRACE/DEBUG/INFO/WARN/ERROR/FATAL).
_SEVERITY_TEXT_REMAP = {
    "WARNING": "WARN",
    "CRITICAL": "FATAL",
}


def _normalize_severity_text(text):
    """Remap stdlib level names to OTel's closed vocabulary where they differ."""
    return _SEVERITY_TEXT_REMAP.get(text, text) if text else "INFO"


def log_row_to_log_records(row):
    """Convert one ``logs_waiting`` row into a list of ReadableLogRecord.

    Returns an empty list when the row's ``attributes`` field is unparseable.
    The returned record carries trace_id/span_id derived from the row's own
    session_id and future_id so log records correlate with their parent span in the
    receiver.
    """
    row = dict(row)

    try:
        attrs = json.loads(row.get("attributes") or "{}")
    except (json.JSONDecodeError, TypeError):
        return []

    trace_id = trace_id_from_session(row.get("session_id"))
    span_id = span_id_from_future(row.get("future_id"))

    resource = Resource(
        {"service.name": row.get("agent_id") or "unknown_agent"}
    )

    # CanyonOS-specific identity fields are namespaced as canyonos.* per the OTel
    # naming spec's app-name-prefix rule (export-time only, no storage change).
    record_attrs = {
        k: v
        for k, v in {
            "canyonos.agent.id": attrs.get("agent.id"),
            "canyonos.agent.name": attrs.get("agent.name"),
            "canyonos.endpoint": attrs.get("endpoint"),
            "logger.name": attrs.get("logger.name"),
            "exception.type": attrs.get("exception.type"),
            "exception.message": attrs.get("exception.message"),
            "exception.stacktrace": attrs.get("exception.stacktrace"),
        }.items()
        if v is not None
    }

    severity_text = _normalize_severity_text(row.get("severity_text"))
    severity_number = SeverityNumber(row.get("severity_number") or 9)

    log_record = LogRecord(
        timestamp=to_epoch_nanos(row.get("observed_at")),
        observed_timestamp=to_epoch_nanos(row.get("observed_at")),
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=_SAMPLED,
        severity_text=severity_text,
        severity_number=severity_number,
        body=row.get("body"),
        attributes=record_attrs,
    )
    return [ReadableLogRecord(log_record=log_record, resource=resource)]
