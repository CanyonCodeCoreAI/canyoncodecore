"""Converts the `logs` field of a waiting row into OTel ReadableLogRecord objects.

Each entry in the JSON array was written by ventis.utils.log_entry (OTel Log Data Model
shape) and carries SeverityNumber/SeverityText/Body/Attributes. Trace attribution reuses
the same future=span mapping from span_convert: session_id→trace_id (128-bit),
future_id[:8]→span_id (64-bit lossy truncation).

Pure function, no I/O.
"""

import json

from opentelemetry._logs import LogRecord
from opentelemetry._logs.severity import SeverityNumber
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.resources import Resource

from otlp_utils import _SAMPLED, to_epoch_nanos, trace_id_from_session, span_id_from_future

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


def waiting_row_to_log_records(row):
    """Convert one waiting row's ``logs`` JSON into a list of ReadableLogRecord.

    Returns an empty list when the row has no logs or the field is unparseable.
    Each returned record carries trace_id/span_id derived from the future's own
    session_id and future_id so log records correlate with their parent span in the
    receiver.
    """
    row = dict(row)
    logs_json = row.get("logs")
    if not logs_json:
        return []

    try:
        entries = json.loads(logs_json)
    except (json.JSONDecodeError, TypeError):
        return []

    if not entries:
        return []

    # Trace/span attribution — shared helpers ensure the mapping never diverges
    # between signals (both use the same lossy 128-bit→64-bit truncation).
    trace_id = trace_id_from_session(row.get("session_id"))
    span_id = span_id_from_future(row.get("future_id"))

    resource = Resource(
        {"service.name": row.get("name") or row.get("agent_id") or "unknown_agent"}
    )

    records = []
    for entry in entries:
        attrs = entry.get("Attributes") or {}

        # Ventis-specific identity fields are namespaced as ventis.* per the OTel
        # naming spec's app-name-prefix rule (export-time only, no storage change).
        record_attrs = {
            k: v
            for k, v in {
                "ventis.agent.id": attrs.get("agent.id"),
                "ventis.agent.name": attrs.get("agent.name"),
                "ventis.endpoint": attrs.get("endpoint"),
                "logger.name": attrs.get("logger.name"),
                "exception.type": attrs.get("exception.type"),
                "exception.message": attrs.get("exception.message"),
                "exception.stacktrace": attrs.get("exception.stacktrace"),
            }.items()
            if v is not None
        }

        severity_text = _normalize_severity_text(entry.get("SeverityText"))
        severity_number = SeverityNumber(entry.get("SeverityNumber", 9))

        log_record = LogRecord(
            timestamp=to_epoch_nanos(entry.get("Timestamp")),
            observed_timestamp=to_epoch_nanos(entry.get("ObservedTimestamp")),
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=_SAMPLED,
            severity_text=severity_text,
            severity_number=severity_number,
            body=entry.get("Body"),
            attributes=record_attrs,
        )
        records.append(ReadableLogRecord(log_record=log_record, resource=resource))

    return records
