"""Shared low-level helpers for the OTLP conversion pipeline.

Centralises the three pieces that both span_convert and log_convert
used to duplicate: epoch-nanosecond conversion, the sampled trace
flag, and the session_id/future_id → OTel trace_id/span_id mapping.
"""

from opentelemetry.trace import TraceFlags

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)


def to_epoch_nanos(unix_seconds):
    """Convert a unix-epoch-seconds float to OTel's nanosecond integer."""
    if unix_seconds is None:
        return None
    return round(float(unix_seconds) * 1e9)


def trace_id_from_session(session_id):
    """Derive a 128-bit OTel trace_id from a Ventis session_id hex string."""
    if not session_id:
        return None
    return int(session_id, 16)


def span_id_from_future(future_id):
    """Derive a 64-bit OTel span_id from a Ventis future_id hex string.

    This is a lossy truncation (128-bit → 64-bit). Both converters must use
    this helper so the mapping cannot silently diverge between signals.
    """
    if not future_id:
        return None
    return int.from_bytes(bytes.fromhex(future_id)[:8], "big")
