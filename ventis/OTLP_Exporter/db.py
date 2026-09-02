"""SQLite schema and writes for the OTel export pipeline's waiting table.

`waiting` holds future rows as GlobalController observes them (including still-running
ones). There's no separate queue table -- OTel's own BatchSpanProcessor already queues
and batches spans in memory, so the only thing we need to track durably is which rows
have already been sent, which the `sent` column on this same table provides. (An earlier
version of this pipeline had a second `queue` table for that; collapsed away since it
wasn't doing anything BatchSpanProcessor doesn't already do -- see DESIGN.md.)
"""

import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_queue.db")

# Bump whenever _TABLE_COLUMNS gains a new column. Old databases are migrated
# once (on first startup after upgrade) and then skipped on every run after.
_SCHEMA_VERSION = 2

# Table schema
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
    name TEXT,
    input TEXT,
    output TEXT,
    logs TEXT,
    sent BOOLEAN DEFAULT 0,
    logs_sent BOOLEAN DEFAULT 0
"""

def init_db(db_path=DB_PATH):
    """Create the waiting table and run any pending schema migrations."""
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(f"CREATE TABLE IF NOT EXISTS waiting ({_TABLE_COLUMNS})")

        if version < _SCHEMA_VERSION:
            # v1 → v2: added logs TEXT and logs_sent BOOLEAN columns.
            if version < 2:
                for col, defn in (("logs", "TEXT"), ("logs_sent", "BOOLEAN DEFAULT 0")):
                    try:
                        conn.execute(f"ALTER TABLE waiting ADD COLUMN {col} {defn}")
                    except sqlite3.OperationalError:
                        pass  # fresh database already has the column from CREATE TABLE
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

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
    "name", "input", "output", "logs",
    # sent and logs_sent are intentionally excluded — re-upserting a row must
    # never reset either flag back to unsent.
]

_WAITING_UPSERT = """
    INSERT INTO waiting ({cols}) VALUES ({placeholders})
    ON CONFLICT(future_id) DO UPDATE SET {updates}
""".format(
    cols=", ".join(_COLUMNS),
    placeholders=", ".join(f":{c}" for c in _COLUMNS),
    updates=", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "future_id"),
)


def _normalize_json_text(value):
    """Return JSON text, encoding legacy scalar strings that are not valid JSON."""
    if value is None or value == "":
        return None
    try:
        json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return json.dumps(value)
    return value


def write_waiting_rows(rows, project_id=None, db_path=DB_PATH):
    """Upsert future rows (as returned by telemetry_logging.pull_runtime_information)
    into the waiting table. Unlike runtime_information, rows without finished_at are
    kept (not skipped) -- that's what "waiting" means here.

    Cost fields (server_cost, token_cost, total_cost) are pre-computed by the
    caller and passed in via each row's 'server_cost'/'token_cost' keys."""
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

            token_cost = float(raw.get("token_cost") or 0.0)
            server_cost = float(raw.get("server_cost") or 0.0)

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
                    "error_name": raw.get("error"),  # field is 'error' on this branch; holds the type name
                    "error_message": raw.get("error") or raw.get("error_message"),
                    "name": name or agent_id or "unknown_agent",
                    "input": _normalize_json_text(raw.get("args")),
                    "output": _normalize_json_text(result),
                    "logs": raw.get("logs") or None,
                },
            )
        conn.commit()
    finally:
        conn.close()


def mark_sent(future_id, db_path=DB_PATH):
    """Mark one waiting row's span as sent."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE waiting SET sent = 1 WHERE future_id = ?", (future_id,))
        conn.commit()
    finally:
        conn.close()


def mark_logs_sent(future_id, db_path=DB_PATH):
    """Mark one waiting row's logs as sent (tracked separately from the span)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE waiting SET logs_sent = 1 WHERE future_id = ?", (future_id,))
        conn.commit()
    finally:
        conn.close()
