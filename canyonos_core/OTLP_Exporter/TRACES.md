# Traces — the `traces_waiting` table and future→span conversion

The trace signal turns each CanyonOS **future** (one agent/method execution) into one OTel
**span**, grouped into a trace per top-level request. See [DESIGN.md](./DESIGN.md) for the
shared pipeline; this doc covers only the trace-specific parts.

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

Read query: `SELECT * FROM traces_waiting WHERE finished_at IS NOT NULL AND (sent IS NULL OR sent
= 0) LIMIT MAX_SPANS_PER_POLL` (512) — a backlog drains over successive polls.

## Row → span conversion (`trace_convert.py`)

Ids are derived deterministically (no hashing) — both `future_id` and `session_id` are
`uuid4().hex` (32 hex / 16 bytes):

```python
trace_id       = int(row["session_id"], 16)                                  # 128-bit, fits
span_id        = int.from_bytes(bytes.fromhex(row["future_id"])[:8], "big")  # 64-bit, truncated
parent_span_id = int.from_bytes(bytes.fromhex(row["parent_id"])[:8], "big") if row["parent_id"] else None
```

Spans are built as plain `ReadableSpan` objects (no `Tracer`/`TracerProvider`/`IdGenerator`).
Failed rows get a hand-built `exception` event (using the SDK's `EXCEPTION_TYPE`/
`EXCEPTION_MESSAGE` keys) plus `Status(StatusCode.ERROR, ...)`. Successful spans use
`STATUS_CODE_UNSET` (OTel reserves `OK` for validated success).

### Attribute naming
- Real OTel GenAI semconv keys: `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`.
- Langfuse keys for payloads: `langfuse.observation.input` / `langfuse.observation.output`.
- Plain names kept where no semconv exists: `cpu`, `gpu`, `execution_time_ms`,
  `queue_time_ms`, `token_count` (infra concepts / an input+output sum with no spec key).

See [SCHEMA.md](./SCHEMA.md) for the full downstream `otel_spans` / `otel_span_attributes`
storage schema at the receiver.

## Poison-row handling
Each converted span is test-encoded individually (`_reject_unexportable`) so one bad row
(out-of-range id, unencodable attribute) can't discard a whole batch. After
`MAX_ROW_EXPORT_ATTEMPTS` (5) rejections a row is replaced by a placeholder span
(`trace_convert.invalid_row_placeholder_span`): named `canyonos.invalid_span`, carrying
`canyonos.export.invalid` + the real `future_id`, keeping the real `trace_id` and masking
out-of-range ids into valid ones. It has **no** exception event — it records that telemetry
could not be represented, not that the agent failed. The attempt counter is in-memory only
(the placeholder makes the outcome durable); only per-row rejections count, not shared batch
failures.

## Delivery specifics
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
