"""Consumer side of the OTel export queue: the bookkeeping writes the exporter makes as it
drains the SQLite queue -- marking rows sent, flushing, and pruning aged-out rows.

Runs inside the ``otel_exporter`` subprocess. Selecting pending rows is done directly in
``otel_exporter.py``; this module holds the mutations that only the exporter can make,
because only it knows which rows were actually delivered. New rows are written by the
producer (``controller/utils/otel_writer.py``); the two sides share nothing but
``controller/utils/schema.py`` and the database file.
"""

import logging
import sqlite3
import time

from canyonos_core.controller.utils.schema import DB_PATH, _execute

logger = logging.getLogger(__name__)


def trace_mark_sent(future_id, db_path=DB_PATH):
    """Mark one traces_waiting row sent. Atomic operation."""
    _execute(
        "UPDATE traces_waiting SET sent = 1 WHERE future_id = ?", (future_id,), db_path
    )


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


def log_mark_sent(log_id, db_path=DB_PATH):
    """Mark one logs_waiting row sent. Atomic operation."""
    _execute("UPDATE logs_waiting SET sent = 1 WHERE log_id = ?", (log_id,), db_path)


def log_mark_sent_many(log_ids, db_path=DB_PATH):
    """Mark every listed logs_waiting row sent in one transaction."""
    if not log_ids:
        return
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        # Re-raised, not swallowed: the caller reports these rows as delivered
        # but unmarked, which is what tells an operator to expect duplicates.
        logger.error(
            "Failed to open %s to mark %d log row(s) sent: %s",
            db_path,
            len(log_ids),
            e,
            exc_info=True,
        )
        raise
    try:
        conn.executemany(
            "UPDATE logs_waiting SET sent = 1 WHERE log_id = ?",
            [(log_id,) for log_id in log_ids],
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


def prune_expired(table, ts_column, older_than_seconds, db_path=DB_PATH):
    """Delete rows older than ``older_than_seconds`` (measured on ``ts_column``, unix
    seconds), returning the number removed -- whether or not they were sent, so a
    destination that stays down/rejecting can't grow the queue file without bound.
    ``table``/``ts_column`` are internal literals (never user input). Rows with a NULL
    timestamp are left alone, so in-flight traces (NULL ``finished_at``) are never
    dropped mid-execution.
    """
    cutoff = time.time() - older_than_seconds
    return _execute(
        f"DELETE FROM {table} "
        f"WHERE {ts_column} IS NOT NULL AND {ts_column} < ?",
        (cutoff,),
        db_path,
    )
