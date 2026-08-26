"""SQLite schema and writes for the OTel export pipeline's waiting table.

`waiting` holds future rows as GlobalController observes them (including still-running
ones). There's no separate queue table -- OTel's own BatchSpanProcessor already queues
and batches spans in memory, so the only thing we need to track durably is which rows
have already been sent, which the `sent` column on this same table provides. (An earlier
version of this pipeline had a second `queue` table for that; collapsed away since it
wasn't doing anything BatchSpanProcessor doesn't already do -- see DESIGN.md.)
"""

import os
import sqlite3

from ventis.controller.utils import pricing

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_queue.db")

# Demo-only multipliers for scaling displayed costs; not real recorded costs. Kept
# deliberately standalone/duplicated from telemetry_logging.py's identical constants
# (rather than importing them) so this module has no dependency on it -- keep these in
# sync by hand if the multipliers there ever change.
_TOKEN_COST_MULTIPLIER = 10000
_SERVER_COST_MULTIPLIER = 100000

# Timestamps are stored as unix epoch seconds (matching the Redis future hash fields
# they're read from), not as SQLite datetime strings. Column set mirrors
# runtime_information 1:1 (see telemetry_logging.py) plus this pipeline's own additions
# (error_name/error_message/sent).
_TABLE_COLUMNS = """
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent BOOLEAN DEFAULT 0
"""


def init_db(db_path=DB_PATH):
    """Create the waiting table if it doesn't already exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"CREATE TABLE IF NOT EXISTS waiting ({_TABLE_COLUMNS})")
        conn.commit()
    finally:
        conn.close()


# `sent` is deliberately excluded here so re-upserting a waiting row (e.g. GC
# re-writing it from Redis) never resets it back to unsent.
_COLUMNS = [
    "future_id", "parent_id", "session_id", "project_id", "agent_id", "model",
    "cpu", "gpu", "started_at", "finished_at", "execution_time_ms", "queue_time_ms",
    "input_token_count", "output_token_count", "token_count", "errors",
    "failed", "server_cost", "token_cost", "total_cost",
    "cached_tokens", "cache_hit_ratio", "error_name", "error_message",
]

_WAITING_UPSERT = """
    INSERT INTO waiting ({cols}) VALUES ({placeholders})
    ON CONFLICT(future_id) DO UPDATE SET {updates}
""".format(
    cols=", ".join(_COLUMNS),
    placeholders=", ".join(f":{c}" for c in _COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "future_id"),
)


def write_waiting_rows(rows, redis_client=None, project_id=None, db_path=DB_PATH):
    """Upsert future rows (as returned by telemetry_logging.pull_runtime_information)
    into the waiting table. Unlike runtime_information, rows without finished_at are
    kept (not skipped) -- that's what "waiting" means here. `redis_client` is only used
    to look up the executing agent's instance type for server-cost pricing, mirroring
    send_runtime_information; pass None to skip cost lookups (server_cost stays 0)."""
    if not rows:
        return
    conn = sqlite3.connect(db_path)
    try:
        for raw in rows:
            fid = raw.get("future_id")
            session_id = raw.get("request_id")
            if not fid or not session_id:
                continue
            agent_id = raw.get("agent")
            started_at = float(raw.get("created_at") or 0) or None
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

            token_cost = (
                pricing.compute_token_cost(
                    raw.get("model"), input_token_count, output_token_count
                )
                * _TOKEN_COST_MULTIPLIER
            )
            # Server cost needs an elapsed duration -- only available once finished.
            if finished_at and started_at:
                server_cost = (
                    pricing.compute_server_cost(
                        redis_client.get(f"agent:{agent_id}:instance_type")
                        if redis_client is not None and agent_id
                        else None,
                        finished_at - started_at,
                    )
                    * _SERVER_COST_MULTIPLIER
                )
            else:
                server_cost = 0.0

            conn.execute(
                _WAITING_UPSERT,
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
                    "error_message": raw.get("error_message"),
                },
            )
        conn.commit()
    finally:
        conn.close()


def mark_sent(future_id, db_path=DB_PATH):
    """Mark one waiting row sent. Call this immediately after successfully handing its
    span to the batch processor -- one row, one commit -- so a crash between two rows'
    sends can't leave an already-sent row unmarked (which would cause a duplicate send
    on the next run)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE waiting SET sent = 1 WHERE future_id = ?", (future_id,))
        conn.commit()
    finally:
        conn.close()
