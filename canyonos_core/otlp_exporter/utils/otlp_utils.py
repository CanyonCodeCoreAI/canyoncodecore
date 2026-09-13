"""Shared low-level helpers for the OTLP conversion pipeline.

Centralises the pieces that both span_convert and log_convert
used to duplicate: epoch-nanosecond conversion, the sampled trace
flag, and the session_id → OTel trace_id mapping.
"""

from opentelemetry.trace import TraceFlags

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)


def to_epoch_nanos(unix_seconds):
    """Convert a unix-epoch-seconds float to OTel's nanosecond integer."""
    if unix_seconds is None:
        return None
    return round(float(unix_seconds) * 1e9)


def trace_id_from_session(session_id):
    """Derive a 128-bit OTel trace_id from a CanyonOS session_id hex string."""
    if not session_id:
        return None
    return int(session_id, 16)
