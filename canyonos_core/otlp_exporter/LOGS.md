# Logs

The log signal ships per-future log records -- captured while a future runs -- as OTel
LogRecords correlated to their parent trace/span. This doc covers only the log-specific parts.

## Flow

```
producers:
  (failures)  Future._submit_request / _mark_future_failed  ─►  build_failure_entry
  (ambient)   LogHandler (controller/utils/log_handler.py)  ─►  build_log_entry
  ─►  both append into the future's Redis `future:{future_id}` hash, field `logs`
      (OTel Log Data Model shape, via controller/utils/log_entry.py)
  ─►  GC explodes each future's `logs` array into one row per record in a SQLite `logs_waiting` table
  ─►  OTLP Exporter reads rows from this table, converts data, and sends it to external OTLP receiver
```

`log_handler.py` and `log_entry.py` are the producers: `LogHandler` is a `logging.Handler`
attached to the root logger that streams DEBUG/INFO records onto the currently-executing
future, while `build_failure_entry` unconditionally records WARNING-and-above failures --
the two are split strictly by severity so neither can double-record the same event.

## Row → LogRecord conversion (`log_convert.py`)

```python
trace_id       = session_id   # 128-bit
span_id        = future_id    # 64-bit, lossy truncation of the future_id hex string
severity_text  = severity_text  # stdlib levelname remapped to OTel's closed vocabulary
```

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
