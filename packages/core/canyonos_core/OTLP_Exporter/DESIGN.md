# OTLP Exporter for CanyonOS GlobalController — Design

Status: **implemented (single-table design; multi-destination fan-out in progress)**. `GlobalController` writes futures into a
`waiting` table (SQLite); a GC-supervised, GC-restarted OTel Exporter process reads
finished/unsent rows, converts them to OTel spans, and exports them synchronously via
`OTLPSpanExporter.export()`, marking rows sent only on a `SpanExportResult.SUCCESS` from
every destination. Serialization, transport, and transient-failure retry are all OTel SDK
code — the only custom pieces are the row→span conversion and durable sent-tracking. This doc is a design/rationale reference; the actual files
(`otel_exporter.py`, `db.py`, `convert.py`, `canyonos/controller/utils/process_supervisor.py`)
are the source of truth for current behavior.

## Context
CanyonOS futures need to reach an external OTLP-compatible tracing backend. Design: a
separate OTLP Exporter process, spawned and supervised by GlobalController, that reads
unsent finished future rows from a local SQLite DB, converts them into OTel spans, and
exports them through the OTel SDK's own OTLP exporters, which ship them to an
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
  OTLP exporter per destination, picking the gRPC vs HTTP
  exporter class from each destination's `protocol` field. gRPC and HTTP destinations
  may be mixed in the same list. Deliberately vendor-neutral: no backend name
  (Postgres, Langfuse, or otherwise) appears anywhere in `otel_exporter.py`; the
  destination is 100% deploy-time config, set once in `global_controller.yaml` and
  never touched by app code again. The originally-planned `database.url` repurposing
  (below, kept for history) was decided against — env-var configuration is the SDK's
  own idiomatic mechanism, so no exporter-side config plumbing was added, only a
  GC-side YAML→env-var translation. If `otel.destinations` is absent, GlobalController
  logs that no OTel metrics collection will happen and skips starting the exporter
  subprocess entirely. Configuration now reaches the exporter through the
  `otel:destinations` Redis key rather than the `CANYONOS_OTEL_DESTINATIONS` env var
  described above (kept for history): GlobalController writes that key, and every poll
  tick re-reads it and rebuilds the exporters if it changed, so a config reload (SIGHUP)
  reaches this process without a restart. An invalid value is logged and ignored,
  leaving the previous working exporters in place.
- **Data source**: NOT `runtime_information` — a dedicated `waiting` table in its own
  SQLite file (`canyonos/OTLP_Exporter/otel_queue.db`, see `db.py`), written by GC's existing
  `_poll_controllers` *alongside* (not instead of) the existing
  `send_runtime_information` write. Keeps this pipeline's schema/state fully decoupled
  from the dashboard/cost table.
- **Two tables collapsed into one**: an earlier version of this design had a second
  `queue` table (`waiting` → promote → `queue` → drain → send). Collapsed to a single
  `sent` column on `waiting`, which provides the same durability with less code. (The
  original rationale cited `BatchSpanProcessor`'s in-memory queue; that processor has
  since been removed — see "Synchronous export" below — and `waiting` is now the only
  queue in the pipeline, which is what makes the durability property hold at all.)
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
endpoint, headers, and timeout to the SDK. Each destination's `timeout` bounds how long a
single failing export blocks the poll loop, so it is worth setting explicitly rather than
leaning on the SDK default of 10s.

### 2. `canyonos/OTLP_Exporter/otel_exporter.py` — synchronous export
A plain loop, polling every `POLL_INTERVAL_SECONDS` (5s, checked every 1s so SIGTERM
stays responsive), calling `_send_pending()` each tick. At startup it constructs one
independent OTLP exporter per configured destination; each may use a different protocol,
endpoint, headers, and timeout:
- `SELECT * FROM waiting WHERE finished_at IS NOT NULL AND (sent IS NULL OR sent = 0)`,
  bounded by `MAX_SPANS_PER_POLL` so one tick's export request stays a sane size; a
  backlog is drained over successive polls.
- Conversion is per row, isolated in its own try/except, and each span is then
  test-encoded individually (`_reject_unexportable`). Encoding is what actually rejects
  a bad row — an out-of-range id, an unencodable attribute — and it would otherwise
  happen inside the batched `export()` call, where one row's failure discards every
  other span in the batch and the error names only the destination. Rejecting per row
  keeps one bad future from blocking everything behind it and names the offending
  `future_id`.
- After `MAX_ROW_EXPORT_ATTEMPTS` (5) such rejections, that row is replaced by a
  placeholder span (`convert.invalid_row_placeholder_span`) which exports normally and
  marks the row sent, so it leaves the pending set instead of being retried forever and
  occupying part of the `LIMIT` window. The placeholder keeps the row's real `trace_id`
  where the `session_id` allows it, and masks out-of-range ids into valid ones. It is
  named `canyonos.invalid_span`, carries `canyonos.export.invalid` and the real
  `future_id` as attributes, and deliberately has no exception event: it records that
  telemetry could not be represented, **not** that the agent failed, and a dashboard must
  be able to tell those apart. The counter is in-memory only — the placeholder is what
  makes the outcome durable, so no schema change and no attempt column are needed.
  Only per-row rejections count toward it; an export failure is shared by the whole
  batch, and counting those would replace the entire queue with placeholders after a
  spell of receiver downtime.
- The resulting spans are exported as one batch per destination via
  `exporter.export(spans)`, which is **synchronous** and returns a `SpanExportResult`.
  Rows are marked sent only when every destination returned SUCCESS *and* reported no
  partial rejection.
- Retry needs no machinery: a failed batch simply leaves those rows at `sent = 0`, and
  the next poll picks them up. `waiting` is the durable queue. The SDK already retries
  transient failures internally with exponential backoff, bounded by the destination's
  `timeout`, so a returned FAILURE means it genuinely gave up.
- Marking is all-or-nothing across destinations, so one destination failing re-delivers
  to destinations that already accepted the batch. Span ids are deterministic, so those
  duplicates collapse at the backend.
- **`partial_success` is checked, for HTTP destinations only.** A receiver may return
  200/OK while rejecting individual spans; the SDK exporters discard the response body
  and report `SUCCESS` regardless, so `sent` would otherwise mean "the receiver accepted
  the request", not "every span was stored". HTTP exporters are therefore built with a
  `requests.Session` carrying a response hook (`_PartialSuccessRecorder`) that reads
  `partial_success.rejected_spans`, and a non-zero count fails the batch. gRPC exposes no
  equivalent public seam, so gRPC destinations cannot detect partial rejection — the
  exporter logs a warning once when one is configured.
- `protocol` is validated against `SUPPORTED_PROTOCOLS` (`grpc`, `http`,
  `http/protobuf`). It was previously read but never checked, so any other value —
  including a typo or an absent field — silently selected the HTTP exporter.
- A queue that never yields a row is reported. `sqlite3.connect()` creates a missing
  file instead of refusing, so a misdirected `DB_PATH` reads as a permanently idle
  queue rather than an error. After `EMPTY_QUEUE_WARNING_POLLS` consecutive empty
  polls the exporter checks the table's total row count and, if it is still zero,
  warns once naming the path. A queue whose rows are all already exported is a
  normal idle state and stays silent.
- No `TracerProvider` and no `BatchSpanProcessor` are used at all; spans are hand-built
  and handed straight to the exporters.
- Every exporter is shut down on exit. Nothing is buffered in an OTLP exporter (its own
  `force_flush()` is a documented no-op), so there is nothing to lose on shutdown —
  anything unacknowledged is still `sent = 0` and resumes on the next start.

**Why not `BatchSpanProcessor`** (the original design, removed): `on_end()` only enqueues
onto an in-memory queue and returns `None`, so the real send happened later on an SDK
background thread with no way to report back. Rows were marked sent immediately and a
failed export was lost silently and permanently, with the HTTP error stranded in the GC
container log. The in-memory queue also silently dropped spans when full, and lost its
entire contents on SIGKILL — while those rows already read `sent = 1`.

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
- ~~Rows are marked `sent` immediately after `BatchSpanProcessor.on_end()` accepts them,
  before the asynchronous OTLP export is confirmed; a later delivery failure can lose a
  span while leaving `sent = 1`.~~ **Fixed** — export is now synchronous and `sent` is
  written only on a SUCCESS from every destination.
- Spans carry no explicit `resource`/`instrumentation_scope` — would show as
  `service.name=unknown_service` at a real backend.
- Per-destination delivery state is still not tracked: `sent` is one boolean across all
  destinations, so if one of several destinations fails the whole batch is retried
  everywhere and the healthy destinations receive duplicates. Harmless for tracing
  backends (ids are deterministic), but a per-destination table would avoid it.
- `waiting` grows unboundedly: sent rows are never pruned, and futures that never finish
  (`finished_at` never arrives) also stay forever, invisible and un-expiring.
- `error_name` is always `NULL` — CanyonOS's own Redis writer never records a distinct
  exception-type field, only a message string.
- Test coverage is still limited; the waiting-field migration/normalization/conversion
  path is covered, but the exporter process and live OTLP delivery are not.
- Live-receiver coverage is now basic but real: the synchronous-export change was
  verified against a local HTTP receiver (span accepted, real OTLP protobuf received,
  row marked sent) as well as a refused connection (row left unsent and retried). Still
  never verified against a production-grade OTLP backend.
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
