# OTLP Exporter for CanyonOS — Design (high level)

The OTLP Exporter ships CanyonOS observability to external OTLP-compatible backends. 

Global Controller writes to a local SQLite queue, and a separate exporter process converts unsent rows to OTLP format and delivers them.

This document covers the architecture common to every signal. For the signal-specific
schemas, conversions, and delivery rules see:
- **[TRACES.md](./TRACES.md)** — the `traces_waiting` table and future→span conversion.
- **[METRICS.md](./METRICS.md)** — the `metrics_waiting` table and sample→metric conversion.

## Pipeline

```
producers (per signal)  ─►  GlobalController  ─►  SQLite otel_queue.db  ─►  OTLP Exporter subprocess  ─►  external OTLP receiver(s)
                            (single writer)       (traces_waiting + metrics_waiting)   (single reader)
```

- **GlobalController is the single writer.** Every poll cycle it reads producer state from
  Redis and writes rows into `otel_queue.db`.
- **One SQLite file with two tables:** `traces_waiting` and `metrics_waiting`.
- **The exporter is the reader/deliverer.** It polls the tables, converts rows,
  exports them synchronously, and marks rows sent only once delivered. Traces and metrics
  are two passes in the same loop; a third signal (logs) would be a third pass.

## Core decisions

### Process model
The exporter is a separate OS process, spawned by GlobalController.
Rationale: fault isolation from GC's core poll/health loop and independent restart.

### Reloading for new OTel DB Endpoints
Destinations live in the `otel:destinations` Redis key. The exporter re-reads it every poll and
rebuilds its exporters when it changes, so a config reload (SIGHUP) reaches it without a full
restart.

**Flush mode:** the exporter is *always* started, even with no destinations configured. When
no endpoint is configured it runs in flush mode — each poll it discards the queued rows
(`_flush_pending` → `db.flush_all`) instead of exporting, so `traces_waiting`/`metrics_waiting`
can't grow unbounded while producers keep collecting. As soon as a destination appears (hot
reload) it switches to normal export.

HTTP exporters append the per-signal path (`/v1/traces`, `/v1/metrics`), since opentelemetry-python does
not append it when `endpoint=` is passed explicitly. gRPC uses the bare endpoint (the
signal is the gRPC service).

### Durable single-table queue
Each table carries a `sent` column (default `0`); there is no separate promote/drain queue.
A row is durable until delivered. Retry needs no machinery: a failed batch simply leaves
rows at `sent = 0`, and the next poll retries them.

### Synchronous, all-or-nothing export
The exporter uses no `TracerProvider`/`MeterProvider` and no batch processors — it hand-
builds OTLP objects and calls `exporter.export(...)`, which is synchronous and returns a
result code. 

### Shared vs signal-specific code
Both signals share `SELECT` of unsent rows and fan-out + mark. Only the conversion step and a small `deliver` closure
are signal-specific. Producers/builders are per signal (`_trace_build_exporters` /
`_metric_build_exporters`).

### Pruning / retention
`_prune_expired_rows()` runs on its own cadence
(5 min) and deletes
rows that are `sent = 1` and have a non-null timestamp and are older than the
signal's retention. Unsent, pending, recent,
and null-timestamp rows are never touched. (Never-finished trace rows — `finished_at IS
NULL` — are not yet reaped.)

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
