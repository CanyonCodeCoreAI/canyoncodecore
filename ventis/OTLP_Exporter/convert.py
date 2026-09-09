"""Converts a future into an OTel ReadableSpan.

Pure function, no I/O, no batching, no network calls. Futures already finished, so this is just a
conversion.
"""

from opentelemetry.sdk.trace import EXCEPTION_MESSAGE, EXCEPTION_TYPE, Event, ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
from opentelemetry.trace.status import Status, StatusCode

_SAMPLED = TraceFlags(TraceFlags.SAMPLED)


def to_epoch_nanos(unix_seconds):
    """Convert a unix-epoch-seconds float (as stored in waiting) to OTel's ns int."""
    if unix_seconds is None:
        return None
    return round(float(unix_seconds) * 1e9)


def waiting_row_to_span(row):
    """Convert one waiting row (dict-like, column names as keys) into a ReadableSpan.

    Rows without finished_at are accepted but produce a span with end_time=None --
    filtering to finished rows is the caller's responsibility, not this function's.
    """
    # sqlite3.Row supports row["col"] but not row.get("col") -- normalize once so the
    # rest of this function can use .get() freely for optional fields.
    row = dict(row)

    trace_id = int(row["session_id"], 16)
    span_id = int.from_bytes(bytes.fromhex(row["future_id"])[:8], "big")
    parent_id = row.get("parent_id")
    parent_span_id = (
        int.from_bytes(bytes.fromhex(parent_id)[:8], "big") if parent_id else None
    )

    context = SpanContext(
        trace_id=trace_id, span_id=span_id, is_remote=False, trace_flags=_SAMPLED
    )
    parent = (
        SpanContext(
            trace_id=trace_id, span_id=parent_span_id, is_remote=False, trace_flags=_SAMPLED
        )
        if parent_span_id
        else None
    )

    events = []
    status = Status(StatusCode.UNSET)
    if row["failed"]:
        events.append(
            Event(
                name="exception",
                attributes={
                    EXCEPTION_TYPE: row.get("error_name") or "RuntimeError",
                    EXCEPTION_MESSAGE: row.get("error_message") or "",
                },
                timestamp=to_epoch_nanos(row.get("finished_at")),
            )
        )
        status = Status(StatusCode.ERROR, description=row.get("error_message"))

    # Model, token, agent, and cache-read usage use OTel GenAI semantic-convention
    # names (see https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/).
    # total_cost uses gen_ai.usage.cost. The remaining Ventis-specific values (project_id, server/token cost
    # breakdown, cache_hit_ratio) have no GenAI or Langfuse equivalent, so they keep
    # plain names.
    attributes = {
        k: v
        for k, v in {
            "gen_ai.request.model": row.get("model"),
            "cpu": row.get("cpu"),
            "gpu": row.get("gpu"),
            "execution_time_ms": row.get("execution_time_ms"),
            "queue_time_ms": row.get("queue_time_ms"),
            "gen_ai.usage.input_tokens": row.get("input_token_count"),
            "gen_ai.usage.output_tokens": row.get("output_token_count"),
            "token_count": row.get("token_count"),
            "langfuse.observation.input": row.get("input"),
            "langfuse.observation.output": row.get("output"),
            "project_id": row.get("project_id"),
            "gen_ai.agent.id": row.get("agent_id"),
            "error_count": row.get("errors"),
            "server_cost": row.get("server_cost"),
            "token_cost": row.get("token_cost"),
            "gen_ai.usage.cost": row.get("total_cost"),
            "gen_ai.usage.cache_read.input_tokens": row.get("cached_tokens"),
            "cache_hit_ratio": row.get("cache_hit_ratio"),
        }.items()
        if v is not None
    }

    return ReadableSpan(
        name=row.get("name") or row.get("agent_id") or "unknown_agent",
        context=context,
        parent=parent,
        attributes=attributes,
        events=events,
        status=status,
        kind=SpanKind.INTERNAL,
        start_time=to_epoch_nanos(row.get("started_at")),
        end_time=to_epoch_nanos(row.get("finished_at")),
    )
