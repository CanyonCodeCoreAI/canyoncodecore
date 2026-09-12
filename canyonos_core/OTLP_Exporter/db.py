"""SQLite schema and writes for the OTel export pipeline's queue tables."""

import json
import logging
import os
import sqlite3
import time

from canyonos_core.OTLP_Exporter import pricing
# Used to enrich trace rows with server cost at write time; a candidate to move
# receiver-side later so the exporter stays a pure queue drainer.


logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_queue.db")

# Cost lookups can fail on every row of every poll, so each kind is reported once.
_cost_failures_logged = set()


def _log_cost_failure(kind, exc):
    """Report the first failure of each cost-lookup kind; suppress the rest."""
    if kind in _cost_failures_logged:
        return
    _cost_failures_logged.add(kind)
    logger.warning(
        "%s lookup failed; affected rows are recorded with a cost of 0. Further "
        "%s failures are suppressed for the life of this process: %s",
        kind,
        kind,
        exc,
        exc_info=True,
    )

# Demo-only multipliers for scaling displayed costs, DELETE FOR MORE ACCURATE METRICS
_TOKEN_COST_MULTIPLIER = 10000
_SERVER_COST_MULTIPLIER = 100000

# Table schema (spans/traces -- the `traces_waiting` table)
_TRACES_TABLE_COLUMNS = """
    future_id TEXT PRIMARY KEY,
    parent_id TEXT,
    session_id TEXT NOT NULL,
    project_id TEXT,
    agent_id TEXT,
    model TEXT,
    cpu REAL,
    gpu REAL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    execution_time_ms INTEGER,
    queue_time_ms INTEGER,
    input_token_count INTEGER,
    output_token_count INTEGER,
    token_count INTEGER,
    errors INTEGER,
    failed BOOLEAN,
    server_cost REAL,
    token_cost REAL,
    total_cost REAL,
    cached_tokens INTEGER,
    cache_hit_ratio REAL,
    error_name TEXT,
    error_message TEXT,
    name TEXT,
    input TEXT,
    output TEXT,
    sent BOOLEAN DEFAULT 0
"""


# There are two types of metrics being taken: machine and instance metrics.
# Machine-level metrics are a time series; the metric *values* live in a single JSON `metrics`
# blob so new gauges can be added without an ALTER. The two types of metrics are split via the `kind` identifier
# metric_convert branches on `kind` to build the right OTel resource + instruments.
_METRICS_TABLE_COLUMNS = """
    sample_id TEXT PRIMARY KEY,
    kind TEXT,
    agent_id TEXT,
    agent_name TEXT,
    host TEXT,
    port TEXT,
    project_id TEXT,
    observed_at TIMESTAMP,
    metrics TEXT,
    sent BOOLEAN DEFAULT 0
"""


def _execute(query, params=(), db_path=DB_PATH):
    """Open a connection, run one statement, commit, and close -- returning the cursor's
    rowcount. For the simple single-statement writes only: the batch writers keep one
    connection open across many rows, and the ``*_many`` marks re-raise on open failure,
    so those stay custom.
    """
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def init_db(db_path=DB_PATH):
    """Create the traces_waiting and metrics_waiting tables if they don't already exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS traces_waiting ({_TRACES_TABLE_COLUMNS})"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS metrics_waiting ({_METRICS_TABLE_COLUMNS})"
        )
        conn.commit()
    finally:
        conn.close()


# `sent` is deliberately excluded here so re-upserting a traces_waiting row (e.g. GC
# re-writing it from Redis) never resets it back to unsent.
_TRACES_COLUMNS = [
    "future_id", "parent_id", "session_id", "project_id", "agent_id", "model",
    "cpu", "gpu", "started_at", "finished_at", "execution_time_ms", "queue_time_ms",
    "input_token_count", "output_token_count", "token_count", "errors",
    "failed", "server_cost", "token_cost", "total_cost",
    "cached_tokens", "cache_hit_ratio", "error_name", "error_message",
    "name", "input", "output",
]

_TRACES_UPSERT = """
    INSERT INTO traces_waiting ({cols}) VALUES ({placeholders})
    ON CONFLICT(future_id) DO UPDATE SET {updates}
""".format(
    cols=", ".join(_TRACES_COLUMNS),
    placeholders=", ".join(f":{c}" for c in _TRACES_COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in _TRACES_COLUMNS if c != "future_id"),
)


def _normalize_json_text(value):
    """Return JSON text, encoding scalar strings that are not valid JSON."""
    if value is None:
        return None
    try:
        json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return json.dumps(value)
    return value


def trace_write_rows(rows, redis_client=None, project_id=None, db_path=DB_PATH):
    """Upsert future rows (as returned by ``telemetry.pull_telemetry``) into the
    traces_waiting table. Rows without finished_at are kept (not skipped) -- that's what
    "waiting" means
    here. `redis_client` is only used to look up the executing agent's instance type for
    server-cost pricing; pass None to skip cost lookups (server_cost stays 0)."""
    if not rows:
        return
    conn = sqlite3.connect(db_path)
    try:
        for raw in rows:
            fid = raw.get("future_id")
            session_id = raw.get("request_id")
            if not fid or not session_id:
                missing = ", ".join(
                    field
                    for field, present in (("future_id", fid), ("request_id", session_id))
                    if not present
                )
                logger.warning(
                    "Dropping future row missing %s; it will never be exported "
                    "(future_id=%r, request_id=%r)",
                    missing,
                    fid,
                    session_id,
                )
                continue
            agent_id = raw.get("agent")
            started_at = float(raw.get("created_at") or 0)
            finished_at = float(raw["finished_at"]) if raw.get("finished_at") else None
            execution_time_ms = (
                round((finished_at - started_at) * 1000)
                if finished_at and started_at
                else None
            )
            input_token_count = int(float(raw.get("input_token_count") or 0))
            output_token_count = int(float(raw.get("output_token_count") or 0))
            token_count = int(float(raw.get("token_count") or 0))
            cached_tokens = int(float(raw.get("input_cache_tokens") or 0))
            service = raw.get("service")
            method = raw.get("method")
            name = raw.get("name") or ".".join(
                part for part in (service, method) if part
            )
            result = raw.get("result")

            # Cost figures are only meaningful once the future has finished, so skip
            # computing them until then rather than recomputing on every poll.
            if finished_at is not None:
                # Cost lookups can fail independently of the telemetry itself (e.g.
                # no aws_instance_pricing table on a local-provider deployment) --
                # don't let that drop the whole row, just cost it at 0.
                try:
                    token_cost = (
                        pricing.compute_token_cost(
                            raw.get("model"), input_token_count, output_token_count
                        )
                        * _TOKEN_COST_MULTIPLIER
                    )
                except Exception as e:
                    _log_cost_failure("Token cost", e)
                    token_cost = 0.0
                try:
                    server_cost = (
                        pricing.compute_server_cost(
                            redis_client.get(f"agent:{agent_id}:instance_type")
                            if redis_client is not None and agent_id
                            else None,
                            finished_at - started_at,
                        )
                        * _SERVER_COST_MULTIPLIER
                    )
                except Exception as e:
                    _log_cost_failure("Server cost", e)
                    server_cost = 0.0
            else:
                token_cost = 0.0
                server_cost = 0.0

            conn.execute(
                _TRACES_UPSERT,
                {
                    "future_id": fid,
                    "parent_id": raw.get("parent") or None,
                    "session_id": session_id,
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "model": raw.get("model"),
                    "cpu": float(raw.get("cpu_resource") or 0),
                    "gpu": float(raw.get("gpu_resource") or 0),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "execution_time_ms": execution_time_ms,
                    "queue_time_ms": (
                        round(float(raw["queue_time"]) * 1000)
                        if raw.get("queue_time")
                        else None
                    ),
                    "input_token_count": input_token_count,
                    "output_token_count": output_token_count,
                    "token_count": token_count,
                    "errors": int(raw.get("errors") or 0),
                    "failed": bool(int(raw.get("failed") or 0)),
                    "server_cost": server_cost,
                    "token_cost": token_cost,
                    "total_cost": server_cost + token_cost,
                    "cached_tokens": cached_tokens,
                    "cache_hit_ratio": cached_tokens / token_count if token_count else 0.0,
                    "error_name": raw.get("error_name"),
                    "error_message": raw.get("error") or raw.get("error_message"),
                    "name": name or agent_id or "unknown_agent",
                    "input": _normalize_json_text(raw.get("args")),
                    "output": _normalize_json_text(result),
                },
            )
        conn.commit()
    finally:
        conn.close()


def trace_mark_sent(future_id, db_path=DB_PATH):
    """Mark one traces_waiting row sent. Atomic operation."""
    _execute(
        "UPDATE traces_waiting SET sent = 1 WHERE future_id = ?", (future_id,), db_path
    )


# `sent` is excluded from the update set for the same reason as `traces_waiting`: re-upserting a
# sample (GC polling faster than the collector, so it re-reads the same tick) must not
# reset an already-exported row back to unsent.
_METRICS_COLUMNS = [
    "sample_id", "kind", "agent_id", "agent_name", "host", "port",
    "project_id", "observed_at", "metrics",
]

_METRICS_UPSERT = """
    INSERT INTO metrics_waiting ({cols}) VALUES ({placeholders})
    ON CONFLICT(sample_id) DO UPDATE SET {updates}
""".format(
    cols=", ".join(_METRICS_COLUMNS),
    placeholders=", ".join(f":{c}" for c in _METRICS_COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in _METRICS_COLUMNS if c != "sample_id"),
)


def metric_write_rows(rows, db_path=DB_PATH):
    """Upsert metrics samples into metrics_waiting. Kind-agnostic -- GlobalController just
    hands over whatever it read from a Redis hash; all metric interpretation happens later
    in metric_convert.

    Each entry is ``{"kind", "host", "metrics": <hash dict>}`` plus, for instance rows,
    ``"port"``/``"agent_id"``/``"agent_name"``, and optionally ``"project_id"``. The
    producer-stamped ``observed_at`` inside the hash is the metric timestamp;
    ``sample_id = {kind}:{agent_id or host[:port]}:{observed_at_ns}`` so a GC re-poll of the same tick
    upserts instead of duplicating (and a stalled producer never grows the table).
    Per-row isolation: one bad sample never drops the rest of the batch.
    """
    if not rows:
        return
    conn = sqlite3.connect(db_path)
    try:
        for raw in rows:
            try:
                metrics = raw.get("metrics") or {}
                host = raw.get("host")
                if not metrics or not host:
                    continue
                kind = raw.get("kind") or "machine"
                port = raw.get("port")
                agent_id = raw.get("agent_id")
                # Producer-stamped timestamp; fall back to read time if absent.
                observed_at = float(metrics.get("observed_at") or 0) or time.time()
                identity = agent_id or (f"{host}:{port}" if port else host)
                sample_id = f"{kind}:{identity}:{int(observed_at * 1e9)}"
                conn.execute(
                    _METRICS_UPSERT,
                    {
                        "sample_id": sample_id,
                        "kind": kind,
                        "agent_id": agent_id,
                        "agent_name": raw.get("agent_name"),
                        "host": host,
                        "port": str(port) if port is not None else None,
                        "project_id": raw.get("project_id"),
                        "observed_at": observed_at,
                        "metrics": json.dumps(metrics),
                    },
                )
            except Exception as e:
                # Isolate per row so one malformed sample can't lose the whole tick.
                logger.warning("Dropping malformed metrics row (non-fatal): %s", e)
                continue
        conn.commit()
    finally:
        conn.close()


def metric_mark_sent(sample_id, db_path=DB_PATH):
    """Mark one metrics_waiting sample sent. Atomic operation."""
    _execute(
        "UPDATE metrics_waiting SET sent = 1 WHERE sample_id = ?", (sample_id,), db_path
    )


def metric_mark_sent_many(sample_ids, db_path=DB_PATH):
    """Mark every listed metrics_waiting sample sent in one transaction."""
    if not sample_ids:
        return
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        # Re-raised, not swallowed: the caller reports these samples as delivered
        # but unmarked, which is what tells an operator to expect duplicates.
        logger.error(
            "Failed to open %s to mark %d metric sample(s) sent: %s",
            db_path,
            len(sample_ids),
            e,
            exc_info=True,
        )
        raise
    try:
        conn.executemany(
            "UPDATE metrics_waiting SET sent = 1 WHERE sample_id = ?",
            [(sample_id,) for sample_id in sample_ids],
        )
        conn.commit()
    finally:
        conn.close()


def trace_mark_sent_many(future_ids, db_path=DB_PATH):
    """Mark every listed traces_waiting row sent in one transaction."""
    if not future_ids:
        return
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        # Re-raised, not swallowed: the caller reports these rows as delivered
        # but unmarked, which is what tells an operator to expect duplicates.
        logger.error(
            "Failed to open %s to mark %d row(s) sent: %s",
            db_path,
            len(future_ids),
            e,
            exc_info=True,
        )
        raise
    try:
        conn.executemany(
            "UPDATE traces_waiting SET sent = 1 WHERE future_id = ?",
            [(future_id,) for future_id in future_ids],
        )
        conn.commit()
    finally:
        conn.close()


def flush_all(table, db_path=DB_PATH):
    """Delete every row from ``table``, returning the count removed. Used when no OTel
    destination is configured: there is nowhere to export to, so queued telemetry is
    dropped each poll to keep the queue file bounded. ``table`` is an internal literal,
    never user input.
    """
    return _execute(f"DELETE FROM {table}", (), db_path)


def prune_sent(table, ts_column, older_than_seconds, db_path=DB_PATH):
    """Delete already-sent rows older than ``older_than_seconds`` (measured on ``ts_column``,
    unix seconds), returning the number removed. Sent rows are transport residue -- once
    exported they have no further use, and metrics_waiting in particular grows every poll,
    so this keeps the queue file bounded. ``table``/``ts_column`` are internal literals
    (never user input). Rows with a NULL timestamp are left alone.
    """
    cutoff = time.time() - older_than_seconds
    return _execute(
        f"DELETE FROM {table} "
        f"WHERE sent = 1 AND {ts_column} IS NOT NULL AND {ts_column} < ?",
        (cutoff,),
        db_path,
    )
