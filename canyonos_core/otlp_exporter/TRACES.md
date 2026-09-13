# Traces

The trace signal turns each CanyonOS future into an OTel
span, grouped into a trace per top-level request. This doc covers only the trace-specific parts.

## Flow

```
  A future gets finished and stored in the original Redis ─►  Redis future:{future_id} hash ─►
  ─►  GC collects all traces and puts them into a SQLite `traces_waiting` table
  ─►  OTLP Exporter reads rows from this table, converts data, and sends it to external OTLP receiver
```

## Row → span conversion (`trace_convert.py`)

```python
trace_id       = session_id
span_id        = future_id
parent_span_id = parent_id
```

## `traces_waiting` table schema (`db.py`)

| Column | Meaning |
| --- | --- |
| `future_id` (PK) | Execution id → OTel `span_id`. |
| `parent_id` | Parent future → parent `span_id`; NULL for a root span. |
| `session_id` | Request id (`= request_id`) → OTel `trace_id`; `NOT NULL`. |
| `project_id` | Deployment/project id (span attribute). |
| `agent_id` | Executing instance id (span attribute). |
| `model` | LLM model id. |
| `cpu`, `gpu` | Observed resource values. |
| `started_at`, `finished_at` | Unix seconds; `finished_at IS NULL` ⇒ still running, not yet exported. |
| `execution_time_ms`, `queue_time_ms` | Timing. |
| `input_token_count`, `output_token_count`, `token_count` | Token usage. |
| `errors`, `failed` | Error count / failure flag. |
| `server_cost`, `token_cost`, `total_cost` | Cost figures (computed at write time via `pricing.py`; only meaningful once finished). |
| `cached_tokens`, `cache_hit_ratio` | Prompt-cache usage. |
| `error_name`, `error_message` | Exception type/message (type is currently always NULL — CanyonOS records only a message). |
| `name` | Stable logical `service.method` (the span name). |
| `input`, `output` | Request args / result as JSON text. |
| `sent` | Send-tracking, default `0`; set `1` only after delivery to every destination. |

