"""Shared SQLite contract for the OTel export queue.

The global controller (producer) and the ``otel_exporter`` subprocess (consumer) are two
separate processes that share one SQLite file. This module is the only thing they have in
common: the file location, the table definitions, and a small single-statement executor.
GC creates the tables at startup -- and since GC is what spawns the exporter, the schema
always exists before the exporter reads; the exporter also creates-if-missing defensively.

Producer-side writes live in ``controller/utils/otel_writer.py``; consumer-side reads,
marks, and prunes live in ``otlp_exporter/otel_reader.py``. Neither imports the other --
they meet only here and in the database file.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otel_queue.db")

# Table schema (spans/traces -- the `traces_waiting` table)
_CREATE_TRACES_WAITING = """
    CREATE TABLE IF NOT EXISTS traces_waiting (
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
    )
"""


# There are two types of metrics being taken: machine and agent metrics.
# Machine-level metrics are a time series; the metric *values* live in a single JSON `metrics`
# blob so new gauges can be added without an ALTER. The two types of metrics are split via the `kind` identifier
# metric_convert branches on `kind` to build the right OTel resource + instruments.
_CREATE_METRICS_WAITING = """
    CREATE TABLE IF NOT EXISTS metrics_waiting (
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
    )
"""


# One row per log record, exploded from a future's `logs` JSON array; `log_id` is
# `{future_id}:{index}` so a GC re-poll of the same future upserts instead of duplicating.
_CREATE_LOGS_WAITING = """
    CREATE TABLE IF NOT EXISTS logs_waiting (
        log_id TEXT PRIMARY KEY,
        future_id TEXT,
        session_id TEXT,
        project_id TEXT,
        agent_id TEXT,
        observed_at TIMESTAMP,
        severity_number INTEGER,
        severity_text TEXT,
        body TEXT,
        attributes TEXT,
        sent BOOLEAN DEFAULT 0
    )
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
    """Create the traces_waiting, metrics_waiting, and logs_waiting tables if they don't already exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_CREATE_TRACES_WAITING)
        conn.execute(_CREATE_METRICS_WAITING)
        conn.execute(_CREATE_LOGS_WAITING)
        conn.commit()
    finally:
        conn.close()
