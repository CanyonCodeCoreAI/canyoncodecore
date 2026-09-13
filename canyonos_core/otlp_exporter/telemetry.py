"""Telemetry feed for the OTLP pipeline.

Reads per-execution future rows from a node's Redis and queues them into the ``traces_waiting``
table for export. Kept out of ``db.py`` -- which owns only the SQLite schema and writes --
because these functions read Redis, not SQLite. GC's ``_poll_one_instance`` calls
``send_telemetry`` once per poll; the OTLP exporter subprocess then drains ``traces_waiting``.
"""

from canyonos_core.otlp_exporter.db import DB_PATH, trace_write_rows


def pull_telemetry(redis_client):
    """Scan a node's Redis for per-execution future rows; each future's identity and
    execution metrics both live at future:{future_id}.
    """
    rows = []
    for key in redis_client.scan_keys("future:*"):
        if key.endswith(":children") or key.endswith(":consumers"):
            continue
        data = redis_client.hgetall(key)
        if data:
            data["future_id"] = data.get("id") or key.split(":")[1]
            rows.append(data)
    return rows


def send_telemetry(redis_client, project_id=None, db_path=DB_PATH):
    """The single telemetry entry point: pull per-execution future rows from Redis and
    queue them into the ``traces_waiting`` table for OTLP export. All traces and metrics flow
    through the OTLP pipeline.
    """
    trace_write_rows(pull_telemetry(redis_client), redis_client, project_id, db_path)
