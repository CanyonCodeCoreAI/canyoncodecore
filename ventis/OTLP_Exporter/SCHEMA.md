# OTel span storage schema

Each emitted OTel span is stored as one `otel_spans` record. This chart also defines a
one-to-one `otel_span_attributes` projection, linked by the same `span_id`, for the
known exporter attributes. The current receiver retains the raw `attributes` JSONB map;
the child table is the relational schema described in `otel_spans_schema.txt`.

```text
┌───────────────────────────┐          ┌────────────────────────────────┐
│         otel_spans        │          │      otel_span_attributes      │
├───────────────────────────┤          ├────────────────────────────────┤
│ PK  span_id               │────1:1───│ PK/FK  span_id                 │
│     trace_id              │          │        model and agent ID      │
│     parent_span_id        │          │        CPU and GPU              │
│     name                  │          │        timing and token usage  │
│     kind                  │          │        input and output        │
│     start/end time (ns)   │          │        project and error count │
│     status code/message   │          │        server/token/total cost │
│     attributes (JSONB)    │          │        cache tokens/hit ratio  │
│     events (JSONB)        │          └────────────────────────────────┘
└───────────────────────────┘
```

## `otel_spans`

| Column | Meaning |
| --- | --- |
| `span_id` | Unique identifier for this span. |
| `trace_id` | Identifier shared by all spans in the same trace. |
| `parent_span_id` | Parent span; empty for a root span. |
| `name` | Operation name, such as an agent method. |
| `kind` | OTel role of the work; current exporter spans are `SPAN_KIND_INTERNAL`. |
| `start_time_unix_nano` / `end_time_unix_nano` | Raw Unix timestamps in nanoseconds. |
| `status_code` / `status_message` | OTel outcome: normally `STATUS_CODE_UNSET`, or `STATUS_CODE_ERROR` with an error message. |
| `attributes` | Complete raw OTel attribute map (JSONB). |
| `events` | OTel events, including any `exception` event (JSONB). |

## `otel_span_attributes`

This one-to-one projection mirrors every attribute currently emitted by
`convert.waiting_row_to_span()`. Fields are nullable because OTel omits an attribute
whose source value is `None`.

| Group | Columns |
| --- | --- |
| Model and agent | `gen_ai.request.model`, `gen_ai.agent.id` |
| Resources and timing | `cpu`, `gpu`, `execution_time_ms`, `queue_time_ms` |
| Token usage | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `token_count`, `gen_ai.usage.cache_read.input_tokens` |
| Input and output | `langfuse.observation.input`, `langfuse.observation.output` |
| Project and errors | `project_id`, `error_count` |
| Costs | `server_cost`, `token_cost`, `gen_ai.usage.cost` |
| Cache | `cache_hit_ratio` |

The DBML source is [`../otel_spans_schema.txt`](../otel_spans_schema.txt).

Successful spans use `STATUS_CODE_UNSET` rather than `STATUS_CODE_OK` because OpenTelemetry reserves `OK` for application- or operator-validated success, while instrumentation normally sets a status only when it records an error.
