# OTLP Exporter for CanyonOS GlobalController — Design

Status: **implemented (single-table design; multi-destination fan-out in progress)**. `GlobalController` writes futures into a
`waiting` table (SQLite); a GC-supervised, GC-restarted OTel Exporter process reads
finished/unsent rows, converts each to an OTel span, and hands it to a real
`BatchSpanProcessor`/`OTLPSpanExporter`. Batching, serialization, and sending are all
OTel SDK code — the only custom pieces are the row→span conversion and durable
sent-tracking. This doc is a design/rationale reference; the actual files
(`otel_exporter.py`, `db.py`, `convert.py`, `canyonos/controller/utils/process_supervisor.py`)
are the source of truth for current behavior.

## Context
CanyonOS futures need to reach an external OTLP-compatible tracing backend. Design: a
separate OTLP Exporter process, spawned and supervised by GlobalController, that reads
unsent finished future rows from a local SQLite DB, converts them into OTel spans, and
hands them to the OTel SDK's own batching/export machinery, which ships them to an
external OTLP Receiver (out of scope here — assumed to be a separate, already-addressable
service).

Decisions (final status):
- **Process model**: a true separate OS process, spawned and supervised by
  GlobalController (not an in-process thread) — via `ProcessSupervisor`
  (`canyonos/controller/utils/process_supervisor.py`, built): `register`/`start_all` to
  spawn, `check_and_respawn` (called from GC's existing poll tick, guarded on
  `self.running` to avoid a shutdown race) to restart it if it ever dies unexpectedly,
  `terminate_all` (called from GC's `stop()`) to shut it down cleanly. Rationale: fault
  isolation from GC's core polling/health loop and independent restart, at low added
  complexity since SQLite is already the entire hand-off boundary between the two.
- **Config**: implemented via a new `otel:` section in `global_controller.yaml`
  holding a `destinations` list, *not* by making `otel_exporter.py` itself
  config-aware. `GlobalController` serializes that list to JSON and passes it to the
  exporter subprocess as a single `CANYONOS_OTEL_DESTINATIONS` env var via
  `ProcessSupervisor.register(..., env=...)`. The exporter builds one independent
  exporter/`BatchSpanProcessor` pair per destination, picking the gRPC vs HTTP
  exporter class from each destination's `protocol` field. gRPC and HTTP destinations
  may be mixed in the same list. Deliberately vendor-neutral: no backend name
  (Postgres, Langfuse, or otherwise) appears anywhere in `otel_exporter.py`; the
  destination is 100% deploy-time config, set once in `global_controller.yaml` and
  never touched by app code again. The originally-planned `database.url` repurposing
  (below, kept for history) was decided against — env-var configuration is the SDK's
  own idiomatic mechanism, so no exporter-side config plumbing was added, only a
  GC-side YAML→env-var translation. If `otel.destinations` is absent, GlobalController
  logs that no OTel metrics collection will happen and skips starting the exporter
  subprocess entirely. Configuration is read at exporter startup; changing it requires
  a GlobalController/exporter restart.
- **Data source**: NOT `runtime_information` — a dedicated `waiting` table in its own
  SQLite file (`canyonos/OTLP_Exporter/otel_queue.db`, see `db.py`), written by GC's existing
  `_poll_controllers` *alongside* (not instead of) the existing
  `send_runtime_information` write. Keeps this pipeline's schema/state fully decoupled
  from the dashboard/cost table.
- **Two tables collapsed into one**: an earlier version of this design had a second
  `queue` table (`waiting` → promote → `queue` → drain → send). Collapsed once it became
  clear `BatchSpanProcessor` already provides its own in-memory queue — the only thing a
  second table added was durability across the exporter's own process restarts, which a
  `sent` column on `waiting` alone provides just as well, with less code. See `db.py`'s
  module docstring.
- **Span construction**: settled — spans are built as `ReadableSpan` objects directly
  (bypassing `Tracer`/`TracerProvider` entirely, no `IdGenerator` workaround needed for
  either `trace_id` or `span_id`). Confirmed working via `ConsoleSpanExporter` during
  development and via real (though unreachable) OTLP export attempts.

## Implementation summary

### 1. Config
`global_controller.yaml` gains an optional `otel:` section:
```yaml
otel:
  destinations:
    - name: railway
      protocol: grpc       # or http
      endpoint: otlp-pg-receiver.railway.internal:4317
      headers: {}
    - name: langfuse
      protocol: http
      endpoint: https://cloud.langfuse.com/api/public/otel/v1/traces
      headers:
        Authorization: Basic ${LANGFUSE_OTLP_HEADERS}   # deployer pre-encodes public:secret
```
`GlobalController._otel_exporter_env()` translates the `destinations` list into
`CANYONOS_OTEL_DESTINATIONS` and hands it to `ProcessSupervisor.register(
"otel_exporter", ..., env=...)`, which supports an `env` param (merged on top of the
parent process's own environment, not a replacement). If `otel.destinations` is
absent, `_otel_exporter_env()` returns `None` and `GlobalController.__init__` skips
registering the exporter subprocess entirely, logging that no OTel metrics
collection will happen. No shape
validation is duplicated on the GlobalController side (deliberately: keep this side
simple, `otel_exporter.py` itself validates destination shape at subprocess startup,
and raises if invoked directly without `CANYONOS_OTEL_DESTINATIONS` set).

`otel_exporter.py` parses the destination configuration at startup and constructs the
appropriate OTLP exporter for each entry (gRPC or HTTP), passing that destination's
endpoint and headers to the SDK. `BatchSpanProcessor(..., schedule_delay_millis=1000)`
— the flush delay is explicitly overridden from the SDK default (5000ms) to 1000ms;
`max_export_batch_size` is left at the SDK default (512), which already approximates the
original "500 spans" batching ask without any override needed.

### 2. `canyonos/OTLP_Exporter/otel_exporter.py`
A plain loop, polling every `POLL_INTERVAL_SECONDS` (5s, checked every 1s so SIGTERM
stays responsive), calling `_send_pending()` each tick. At startup it constructs one
independent OTLP exporter and `BatchSpanProcessor` for each configured destination;
each pair may use a different protocol, endpoint, and headers:
- `SELECT * FROM waiting WHERE finished_at IS NOT NULL AND (sent IS NULL OR sent = 0)`.
- Per row, each isolated in its own try/except (one malformed row is logged and skipped,
  never blocks the rest of the batch): `convert.waiting_row_to_span(row)` →
  `on_end(span)` on every configured processor → `db.mark_sent(future_id)` immediately.
  The row is marked after it has been queued to all processors. `sent` therefore means
  **queued to every configured destination**, not remotely acknowledged; this is the
  initial best-effort delivery contract and retains the existing single boolean schema.
- Each processor is constructed once at startup; no `TracerProvider` is used at all,
  since spans are hand-built and handed straight to the processors via `on_end()`.
- Every processor is shut down on exit, flushing its pending batch independently.

### 3. Future row → OTel span conversion (`canyonos/OTLP_Exporter/convert.py`)
`future_id` maps to OTel `span_id`, not `trace_id` — `session_id` (== `request_id`) is
the one that maps to `trace_id`. Both are `uuid4().hex` (32 hex chars / 16 bytes); OTel
`trace_id` is 128-bit (16 bytes, fits directly) and `span_id` is 64-bit (8 bytes, needs
truncation). No hashing — just hex-decode and truncate (deterministic, pure):
```python
trace_id = int(row["session_id"], 16)
span_id = int.from_bytes(bytes.fromhex(row["future_id"])[:8], "big")
parent_span_id = int.from_bytes(bytes.fromhex(row["parent_id"])[:8], "big") if row["parent_id"] else None
```
Spans are assembled as plain `ReadableSpan(name=..., context=SpanContext(...), parent=SpanContext(...) or None, attributes=..., events=..., status=..., start_time=..., end_time=...)`
— no `Tracer`, no `IdGenerator`. Failed rows get a hand-built `exception` `Event` (using
the SDK's own `EXCEPTION_TYPE`/`EXCEPTION_MESSAGE` constants from `opentelemetry.sdk.trace`,
not hardcoded strings — `record_exception()` can't be used retrospectively since there's
no live exception object, only strings) plus `Status(StatusCode.ERROR, description=...)`.

**Attribute naming**: `model`/`input_token_count`/`output_token_count` are set under the
real, current OTel GenAI semantic-convention keys — `gen_ai.request.model`/
`gen_ai.usage.input_tokens`/`gen_ai.usage.output_tokens` — verified against the actual
spec (`open-telemetry/semantic-conventions`), not assumed. Submitted `args` and the
completed `result` are stored in `waiting.input`/`waiting.output` as valid JSON text and
exported under Langfuse's documented `langfuse.observation.input`/
`langfuse.observation.output` attributes. The span name is the stable logical
`service.method`, not the executing instance's UUID. `cpu`/`gpu`/
`execution_time_ms`/`queue_time_ms`/`token_count` keep plain names deliberately: none of
them have an OTel GenAI equivalent (cpu/gpu/queue-time are CanyonOS infra concepts, and
`token_count`, an input+output sum, isn't part of the spec at all — inventing a
`gen_ai.*`-shaped name for any of these would fabricate a standard rather than follow
one. `cached_tokens`/`cache_hit_ratio` exist on the `waiting` row but aren't exported to
attributes at all yet — a separate, pre-existing gap, not touched here.

### 4. Process supervisor — `canyonos/controller/utils/process_supervisor.py` (built)
`ProcessSupervisor`: `register(name, argv, env=None)` declares a process spec (`env`,
when given, is merged on top of — not a replacement for — the parent's own environment);
`start_all()` spawns everything registered; `check_and_respawn()` restarts anything that
exited, replaying the same argv/env (called from GC's `_poll_controllers`, guarded by
`if self.running:` so a SIGTERM mid-tick can't cause it to resurrect a process
`terminate_all()` just intentionally killed); `terminate_all()` terminates every managed
process (all `.terminate()` calls first, then `.wait()` on each, falling back to
`.kill()`), called from GC's `stop()`. Adding a future second daemon is one more
`register()` call — no new spawn/monitor/terminate code needed.

### 5. Poll/cleanup race fix (`canyonos/controller/global_controller.py`)
GC's cleanup thread used to run on its own `cleanup_interval` timer (default 10s),
fully independent of the poll loop's `poll_interval` (default 5s) that writes futures
into `waiting`. On a fast-completing request, cleanup could delete a session's Redis
future keys before the next poll tick ever read them, so those futures never reached
`waiting` at all — silently dropped from every OTel destination, not just one.
Reproduced live: a fast request left only 1 of 6 agent calls in `waiting`. Fixed by
having the poll loop signal a `threading.Event` (`_cleanup_ready`) right after each
tick; the cleanup thread waits on that event instead of sleeping on its own timer, so
cleanup only ever runs immediately after a poll has already captured that tick's state.
Cleanup stays on its own thread (the event's `wait(timeout=cleanup_interval)` is a
fallback, not the primary trigger) so a slow/hung instance during cleanup can't stall
the poll loop's health checks and OTel writes.

### 6. Dependencies (all added)
`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`,
`opentelemetry-exporter-otlp-proto-http` (the last one added alongside the `otel:`
config work, since `protocol: http` now needs that package importable).

## Known gaps (not yet built)
- `WriteResult()` passes an undefined `error_message` variable to its fan-out callback,
  which can interrupt remote consumer propagation after the callback hash is persisted.
- Rows are marked `sent` immediately after `BatchSpanProcessor.on_end()` accepts them,
  before the asynchronous OTLP export is confirmed; a later delivery failure can lose a
  span while leaving `sent = 1`.
- Spans carry no explicit `resource`/`instrumentation_scope` — would show as
  `service.name=unknown_service` at a real backend.
- Destination-specific delivery acknowledgement/retry state is not tracked yet:
  `sent` only records that the span was queued to all configured processors, so an
  asynchronous export failure can still lose a span until a later delivery-state design
  is added.
- `waiting` grows unboundedly: sent rows are never pruned, and futures that never finish
  (`finished_at` never arrives) also stay forever, invisible and un-expiring.
- `error_name` is always `NULL` — CanyonOS's own Redis writer never records a distinct
  exception-type field, only a message string.
- Test coverage is still limited; the waiting-field migration/normalization/conversion
  path is covered, but the exporter process and live OTLP delivery are not.
- Never verified against a live OTLP receiver — only against a refused connection
  (confirmed the SDK's real retry/error-handling path is exercised correctly).
- No retry-limit/quarantine for a permanently malformed row — it logs an error every poll
  forever rather than being given up on.

## Verification approach used during development
- Row→span conversion: ad hoc scripts asserting deterministic id derivation, correct
  parent/child linkage, correct `ERROR` status + `exception` event on failed rows, and
  passing hand-built spans through `ConsoleSpanExporter().export([span])` to confirm the
  SDK accepts them without error.
- Pipeline correctness: seeded `waiting` with mixes of finished/still-running/malformed/
  failed rows, ran the real `otel_exporter.py` subprocess, and inspected the resulting
  `sent` flags and log output directly — including confirming a second run does not
  re-send already-sent rows, and that a malformed row is skipped without blocking others.
- Process supervision: unit-tested `ProcessSupervisor` against a dummy process (spawn,
  kill, confirm respawn with a new PID, confirm clean `terminate_all`) and
  integration-tested it managing the real `otel_exporter.py` process.
- Scoped to the local provider throughout — no EC2 needed.
