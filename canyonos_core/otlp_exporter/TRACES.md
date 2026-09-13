# Traces

The trace signal turns each CanyonOS future into an OTel
span, grouped into a trace per top-level request. This doc covers only the trace-specific parts.

## Flow

```
LocalController._execute_locally  ─►  Redis future:{future_id} hash
  ─►  GC._poll_one_instance → telemetry.send_telemetry(node_redis, project_id)
        (pull_telemetry scans future:* → db.trace_write_rows)
  ─►  SQLite `traces_waiting` table
  ─►  OTLP Exporter: _trace_send_pending() reads finished + unsent rows
        (trace_convert.trace_row_to_span → OTLPSpanExporter.export)
  ─►  external OTLP receiver (stored as `otel_spans`; see SCHEMA.md)
```

`send_telemetry` is the single trace feed (`telemetry.py`): it scans `future:*` Redis hashes
(skipping `:children`/`:consumers`), stamps each row's `future_id`, and upserts them via
`trace_write_rows`. Rows missing `future_id`/`request_id` are dropped and logged, not
silently lost. In-flight futures (no `finished_at`) are **kept** in `traces_waiting` — that's what
"waiting" means — and only exported once finished.

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

## Row → span conversion (`trace_convert.py`)

```python
trace_id       = session_id
span_id        = future_id
parent_span_id = parent_id
```

## Specific Info
- Batch exported per destination via synchronous `exporter.export(spans)` → `SpanExportResult`.
- **HTTP partial success is checked**: a receiver may return 200 while rejecting spans, and
  the SDK discards the response body. HTTP exporters carry a `requests.Session` response
  hook (`_PartialSuccessRecorder`) reading `partial_success.rejected_spans`; a non-zero
  count fails the batch. gRPC exposes no equivalent — a warning is logged once when a gRPC
  destination is configured.
- **Empty-queue warning**: after `EMPTY_QUEUE_WARNING_POLLS` consecutive empty polls, if
  `traces_waiting` is genuinely empty, warn once naming the path (`_trace_note_queue_state`).
- **Retention**: sent rows older than `TRACE_RETENTION_SECONDS` (30 min, by `finished_at`)
  are pruned; unsent/pending/recent/null-`finished_at` rows are never removed.
