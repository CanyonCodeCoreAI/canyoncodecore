# Logs

The log signal ships per-future log records -- captured while a future runs -- as OTel
LogRecords correlated to their parent trace/span. This doc covers only the log-specific parts.

There are two specific paths that logs go through, based on severity. Any log marked as an "error" or greater goes through its own path, and is always logged. On the other side, any other logs (genral logs, warnings) are optional (default=True), where if you set logging = False in global_controller.yaml, you will not receive any of these logs. 

Note: If network bandwidth becomes an issue, set logging to False. Logs are bulky.

## Flow

```
producers:
  (failures)  Future._submit_request / _mark_future_failed  ─►  build_failure_entry (always turned on)
  (general)   LogHandler (controller/utils/log_handler.py)  ─►  build_log_entry (optional, can be turned off)
  ─►  both append into the future's Redis `future:{future_id}` hash, field `logs`
      (OTel Log Data Model shape, via controller/utils/log_entry.py)
  ─►  GC explodes each future's `logs` array into one row per record in a SQLite `logs_waiting` table
  ─►  OTLP Exporter reads rows from this table, converts data, and sends it to external OTLP receiver
```

`log_handler.py` and `log_entry.py` are the producers: `LogHandler` is a `logging.Handler`
attached to the root logger that puts DEBUG/INFO records onto the future, while `build_failure_entry` unconditionally records WARNING-and-above failures.

LogHandler ignores errors as they already get logged by `build_failure_entry` so neither can double-record the same event.

Trace/span attribution reuses the same helpers as `trace_convert.py`
(`trace_id_from_session`, `span_id_from_future`) so the mapping cannot diverge between
signals. `severity_text` gets remapped at export time from the stdlib's levelnames to
OTel's 6-name closed vocabulary (`WARNING` → `WARN`, `CRITICAL` → `FATAL`; others pass
through unchanged).

## `logs_waiting` table schema (`controller/utils/schema.py`)

| Column | Meaning |
| --- | --- |
| `log_id` (PK) | `{future_id}:{index}` -- deterministic, so a GC re-poll upserts instead of duplicating. |
| `future_id` | Owning future → OTel `span_id`. |
| `session_id` | Request id → OTel `trace_id`. |
| `project_id` | Deployment/project id. |
| `agent_id` | Executing instance id. |
| `observed_at` | Unix seconds; the prune column. |
| `severity_number` | OTel `SeverityNumber`. |
| `severity_text` | OTel `SeverityText`. |
| `body` | Log message text. |
| `attributes` | JSON blob, same rationale as `metrics_waiting.metrics`. |
| `sent` | Send-tracking, default `0`; set `1` only after delivery to every destination. |

## Endpoint
HTTP log exporters target `<endpoint>/v1/logs`; gRPC uses the bare endpoint. The
configured destination `endpoint` is the OTLP root, shared with the trace and metric
signals, which append their own signal-specific path.
