# OTLP Exporter for CanyonOS — Design (high level)

The OTLP Exporter ships CanyonOS observability to external OTLP-compatible backends. It is
a **poll-driven, retrospective** pipeline: producers write to a local SQLite queue, and a
separate exporter process converts unsent rows to OTLP and delivers them.

This document covers the architecture common to every signal. For the signal-specific
schemas, conversions, and delivery rules see:
- **[TRACES.md](./TRACES.md)** — the `traces_waiting` table and future→span conversion.
- **[METRICS.md](./METRICS.md)** — the `metrics_waiting` table and sample→metric conversion.
- **[SCHEMA.md](./SCHEMA.md)** — the downstream receiver's span storage schema.

> The source files (`otel_exporter.py`, `db.py`, `trace_convert.py`, `metric_convert.py`,
> `telemetry.py`, and `../controller/utils/process_supervisor.py`) are the source of truth
> for current behavior; this is a design/rationale reference.

## Pipeline

```
producers (per signal)  ─►  GlobalController  ─►  SQLite otel_queue.db  ─►  OTLP Exporter subprocess  ─►  external OTLP receiver(s)
                            (single writer)       (traces_waiting + metrics_waiting)   (single reader)
```

- **GlobalController is the single writer.** Every poll tick it reads producer state from
  Redis and writes rows into `otel_queue.db`. Local controllers never touch SQLite.
- **One SQLite file, two tables:** `traces_waiting` (traces) and `metrics_waiting` (metrics),
  created by `db.init_db()` at GC startup (`CREATE TABLE IF NOT EXISTS`).
- **The exporter subprocess is the reader/deliverer.** It polls the tables, converts rows,
  exports them synchronously, and marks rows sent only once delivered. Traces and metrics
  are two passes in the same loop; a third signal (logs) would be a third pass.

## Core decisions

### Process model
The exporter is a real OS process, spawned and supervised by GlobalController via
`ProcessSupervisor` (`register` / `start_all`; `check_and_respawn` from GC's poll tick,
guarded on `self.running` to avoid a shutdown race; `terminate_all` from GC's `stop()`).
Rationale: fault isolation from GC's core poll/health loop and independent restart, cheap
because SQLite is already the entire hand-off boundary. Adding a daemon is one more
`register()` call.

### Configuration
Destinations live in the `otel:destinations` Redis key (written by GC from the `otel:`
section of `global_controller.yaml`). The exporter re-reads that key **every poll tick** and
rebuilds its exporters when it changes, so a config reload (SIGHUP) reaches it without a
restart; an invalid value is logged and ignored, leaving the working exporters in place.
Configuration is deliberately vendor-neutral — no backend name appears in `otel_exporter.py`.

**Flush mode:** the exporter is *always* started, even with no destinations configured. When
no endpoint is configured it runs in flush mode — each poll it discards the queued rows
(`_flush_pending` → `db.flush_all`) instead of exporting, so `traces_waiting`/`metrics_waiting`
can't grow unbounded while producers keep collecting. As soon as a destination appears (hot
reload) it switches to normal export.

Each destination has a `name`, a `protocol` (`grpc`, `http`, or `http/protobuf`, validated
against `SUPPORTED_PROTOCOLS`), an `endpoint`, optional `headers`/`timeout`/`insecure`. The
exporter builds one independent OTLP exporter per destination per signal; gRPC and HTTP may
be mixed. **The `endpoint` is the OTLP root** — HTTP exporters append the per-signal path
(`/v1/traces`, `/v1/metrics`), since opentelemetry-python does
not append it when `endpoint=` is passed explicitly. gRPC uses the bare endpoint (the
signal is the gRPC service).

### Durable single-table queue
Each table carries a `sent` column (default `0`); there is no separate promote/drain queue.
A row is durable until delivered. Retry needs no machinery: a failed batch simply leaves
rows at `sent = 0`, and the next poll retries them — `traces_waiting`/`metrics_waiting` *are* the
retry queue.

### Synchronous, all-or-nothing export
The exporter uses no `TracerProvider`/`MeterProvider` and no batch processors — it hand-
builds OTLP objects and calls `exporter.export(...)`, which is synchronous and returns a
result code. The shared `_deliver_and_mark` fans a converted batch out to every destination
and marks the rows sent **only when every destination accepted**. A single failing
destination re-delivers to the healthy ones next poll; deterministic ids/sample_ids collapse
those duplicates at the backend. Per-destination health is tracked so recovery is logged
once. (The earlier `BatchSpanProcessor` design was removed: `on_end()` only enqueued, so a
row was marked sent before the async send was confirmed and a failed export was lost.)

### Shared vs signal-specific code
Both signals share `_read_pending_rows` (bounded `SELECT` of unsent rows) and
`_deliver_and_mark` (fan-out + mark). Only the conversion step and a small `deliver` closure
are signal-specific. Producers/builders are per signal (`_trace_build_exporters` /
`_metric_build_exporters`). The trace feed lives in `telemetry.py` (`send_telemetry`); the
metric feed is GC's per-poll `metric_write_rows`.

### Pruning / retention
Sent rows are transport residue. `_prune_expired_rows()` runs on its own cadence
(`PRUNE_INTERVAL_SECONDS`, 5 min — independent of the poll tick) and deletes, per table,
rows that are `sent = 1` **and** have a non-null timestamp **and** are older than the
signal's retention (`TRACE_RETENTION_SECONDS` 30 min on `traces_waiting.finished_at`,
`METRIC_RETENTION_SECONDS` 10 min on `metrics_waiting.observed_at`). Unsent, pending, recent,
and null-timestamp rows are never touched. (Never-finished trace rows — `finished_at IS
NULL` — are not yet reaped; see gaps.)

### Empty-queue detection
`sqlite3.connect()` creates a missing file rather than failing, so a misdirected `DB_PATH`
reads as a permanently idle queue. Each signal tracks its own consecutive-empty-poll counter
(`_trace_note_queue_state` / `_metric_note_queue_state`) and, after a threshold, confirms the
table is genuinely empty and warns once, naming the path. A fully-exported queue is a normal
idle state and stays silent.

### Poll/cleanup race fix (GC)
GC's cleanup thread waits on a `threading.Event` (`_cleanup_ready`) the poll loop signals
after each tick, instead of running on its own timer — so cleanup never deletes a request's
Redis future keys before the poll that captures them writes them to `traces_waiting`.

## Dependencies
`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`,
`opentelemetry-exporter-otlp-proto-http`.

## Known gaps
- Per-destination delivery state is one `sent` boolean across all destinations, so one
  destination failing re-delivers to the healthy ones (harmless — ids/sample_ids are
  deterministic).
- Never-finished trace rows (`finished_at` never arrives) are never reaped.
- gRPC destinations cannot detect partial rejection (no public seam); HTTP can.
- Live delivery has been verified against a local receiver, not a production backend.
