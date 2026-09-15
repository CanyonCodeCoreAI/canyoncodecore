"""Shared low-level helpers for the OTLP conversion pipeline."""

from opentelemetry.trace import TraceFlags

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)


def to_epoch_nanos(unix_seconds):
    """Convert a unix-epoch-seconds float to OTel's nanosecond integer."""
    if unix_seconds is None:
        return None
    return round(float(unix_seconds) * 1e9)
