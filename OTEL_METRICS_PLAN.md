# CAN-312: Replace custom metrics logging with OTel Metrics

Status: **planning / hardening — no code yet**
Branch: `CAN-312-OTel-Metrics`

## 1. Goal

Retire the custom (SQLAlchemy → Postgres `agent_information`) metrics path and emit
per-instance metrics as a real **OTel Metrics** signal, reusing the existing
GC-owned-SQLite → supervised-exporter-subprocess architecture that traces already use.

Non-goals: touching the trace/span pipeline, replacing the `session` /
`runtime_information` business tables, or building live in-process metric instruments on
the local controllers. Metrics stay a *retrospective, poll-driven* signal (same as spans
today), not live push.

## 2. Current state

### 2.1 Trace pipeline (OTel, already built — the template we copy)
```
LocalController._execute_locally → Redis future:{id} hash
  → GC._poll_one_instance → pull_runtime_information() (scan future:*)
  → GC._otel_db.write_waiting_rows() → SQLite `waiting` table (OTLP_Exporter/otel_queue.db)
  → OTLP_Exporter subprocess (_send_pending) reads finished + unsent rows
  → convert.waiting_row_to_span() → exporter → external OTLP receiver
```
- GC is the **single writer** to `otel_queue.db`; the exporter subprocess is the only reader.
- Destinations come from Redis key `otel:destinations`, re-read every poll tick (SIGHUP-free reload).
- The exporter is spawned + supervised by `ProcessSupervisor` (`register` / `start_all` /
  `check_and_respawn` / `terminate_all`).

> Note: on this branch `otel_exporter.py` still uses `BatchSpanProcessor` + per-row
> `on_end()` + per-row `db.mark_sent()`. The `canyoncodecore` main line has since moved
> spans to synchronous batched `exporter.export()` + `mark_sent_many()` + poison-row
> rejection (CAN-334). Decide early whether metrics target the old design on this branch
> or we rebase onto the newer span pipeline first (see §8, item H).

### 2.2 Metrics pipeline (custom — the one being replaced)
```
LocalController._metrics_loop (thread, ~5s) → _collect_metrics()
  → Redis hash controller:{host}:{port}:metrics  (cpu/gpu/disk/memory %, queue_length, uptime, status)
LocalController._execute_locally → redis.hincrby(metrics_key, "requests_served" / "full_failures" / "error_count")
  → GC._poll_one_instance drains the hash, computes throughput
  → telemetry_logging.send_agent_information() → SQLAlchemy UPSERT → Postgres `agent_information`
  → GC resets requests_served/full_failures/error_count to 0 on the hash
```
The local controllers already do exactly what we need on the producer side: publish a
metrics hash + increment counters in Redis. **No producer-side transport work is required
—** GC polls and reads the results, as the user specified.

Key files:
- `controller/local_controller.py` — `_collect_metrics`, `_metrics_loop`, `_execute_locally` (producer; largely unchanged)
- `controller/global_controller.py:596–685` — `_poll_one_instance` metrics drain block (rewire)
- `controller/utils/telemetry_logging.py` — `send_agent_information`, `AGENT_TABLE_NAME`, `_AGENT_UPSERT` (retire)
- `OTLP_Exporter/db.py`, `convert.py`, `otel_exporter.py` (extend for metrics)

## 3. Target architecture

Mirror the trace pipeline exactly, adding a **second SQLite table** and a **metrics
export path** in the same subprocess:
```
LocalController (unchanged): metrics hash + counters in Redis
  → GC._poll_one_instance reads the metrics hash (as it does now)
  → GC writes a metrics row → SQLite `metrics_waiting` table (same otel_queue.db)
  → OTLP_Exporter subprocess: new _send_pending_metrics() reads unsent rows
  → convert.metrics_row_to_datapoints() → OTLPMetricExporter (grpc/http) → external receiver
```
- GC stays the single SQLite writer. Local controllers never touch SQLite.
- One `otel_queue.db` file, two tables (`waiting` for spans, `metrics_waiting` for metrics).
- One subprocess, two poll passes (spans + metrics) — or two exporter targets built from
  the same `otel:destinations` config.

## 4. New SQLite table: `metrics_waiting`

Metrics are per-*instance heartbeat samples*, not per-*execution*, so the span-shaped
`waiting` table cannot be reused. Proposed columns (final shape TBD in review):

| Column | Meaning |
| --- | --- |
| `sample_id` (PK) | Synthetic id per (instance, poll-tick) so re-polls upsert instead of duplicate. e.g. `{agent_id}:{observed_at_ns}` |
| `agent_id` | Replica instance id (resource attribute) |
| `agent_name` | Logical agent name (resource attribute) |
| `project_id` | From GC config (resource attribute) |
| `host`, `port` | Instance location (resource attributes) |
| `observed_at` | Unix seconds at which GC read the hash (metric timestamp) |
| `cpu_percent`, `gpu_percent`, `disk_percent`, `memory_percent` | Gauge values |
| `queue_length` | Gauge value |
| `uptime_seconds` | Gauge value |
| `health` / `status` | Gauge/state or resource attribute |
| `requests_served` | Counter delta drained this tick |
| `full_failures` | Counter delta drained this tick |
| `error_count` | Counter delta drained this tick |
| `sent` (default 0) | Durable send-tracking, same contract as `waiting.sent` |

- `init_db()` must `CREATE TABLE IF NOT EXISTS metrics_waiting (...)` alongside `waiting`,
  run synchronously at GC startup before either process touches the file
  (`global_controller.py:156`), for the same reason `waiting` is.
- Write path: new `db.write_metrics_rows(rows, ...)` mirroring `write_waiting_rows`.
- Read path: new `_send_pending_metrics()` selecting `sent = 0` rows; mark sent only on
  successful export.

## 5. Metric semantic mapping (custom → OTel)

| Custom field | OTel instrument | OTel/semconv name (proposed) | Notes |
| --- | --- | --- | --- |
| cpu_percent | Gauge | `system.cpu.utilization` (0–1) or `canyonos.instance.cpu.percent` | decide unit: percent vs fraction |
| gpu_percent | Gauge | `canyonos.instance.gpu.percent` | no stable semconv; keep namespaced |
| disk_percent | Gauge | `system.filesystem.utilization` or namespaced | |
| memory_percent | Gauge | `system.memory.utilization` or namespaced | |
| queue_length | Gauge (UpDownCounter-like) | `canyonos.instance.queue.length` | |
| uptime_seconds | Gauge | `canyonos.instance.uptime` | |
| requests_served | **Sum (monotonic)** | `canyonos.instance.requests` | **temporality decision — see §8.A** |
| full_failures | Sum (monotonic) | `canyonos.instance.failures` | |
| error_count | Sum (monotonic) | `canyonos.instance.errors` | always 0 today (drained/reset before it accumulates) — verify it's still meaningful |
| throughput | **dropped** | — | derivable from `requests` counter rate at the backend; stop computing it in code |
| health/status | resource attr or state gauge | `canyonos.instance.health` | |
| agent_id/name, project_id, host, port | **resource attributes** | `service.name`, `canyon.project.id`, etc. | |

## 6. Component-by-component changes

1. **`OTLP_Exporter/db.py`** — add `metrics_waiting` schema to `init_db()`, add
   `write_metrics_rows()`, add `mark_metrics_sent()` (or reuse a table-parameterized helper).
2. **`OTLP_Exporter/convert.py`** — add `metrics_row_to_datapoints(row)` producing OTel
   metric data points (Gauge for the %s, Sum for the counters) with a shared `Resource`.
3. **`OTLP_Exporter/otel_exporter.py`** — build `OTLPMetricExporter` (grpc + http variants)
   per destination alongside the span exporter; add a `_send_pending_metrics()` pass to the
   poll loop; shut metric exporters down on exit.
4. **`controller/global_controller.py`** — in `_poll_one_instance`, replace the
   `send_agent_information(...)` call with `self._otel_db.write_metrics_rows(...)`. Keep the
   counter drain-and-reset. Drop the GC-side throughput computation (or keep transiently).
5. **`controller/utils/telemetry_logging.py`** — retire `send_agent_information`,
   `AGENT_TABLE_NAME`, `_AGENT_UPSERT` (and now-dead imports) once the OTel path is verified.
   `pull_runtime_information` stays (feeds spans).
6. **`controller/local_controller.py`** — largely unchanged. Only touch if we decide the
   metrics hash needs extra fields (e.g. explicit `observed_at`, or splitting counters from gauges).
7. **Config/docs** — extend `OTLP_Exporter/SCHEMA.md` + `DESIGN.md` for the metrics table
   and signal; confirm `otel:destinations` covers a metrics endpoint (`/v1/metrics` for HTTP).

## 7. Answer to "what else" — decisions needed & hardening (the important part)

Lessons folded in from the CAN-334 span-exporter hardening (much of this pipeline's
failure modes are already documented there and will recur for metrics).

### A. Counter temporality — the single easiest thing to get wrong
GC currently **resets** requests_served/full_failures/error_count to 0 after each drain,
so what GC sees is a **delta**, not a cumulative total. OTel `Sum` instruments are
cumulative-monotonic by default. We must pick one explicitly:
- **Delta temporality** on the metric exporter (feed the drained delta directly), or
- **Accumulate** a running total before writing the row (feed cumulative).
Getting this wrong silently corrupts request/error counts. Also confirm each destination
(Langfuse/Postgres receiver/collector) accepts the chosen temporality.

### B. Metrics are a different OTLP signal from traces
A receiver accepting spans at `/v1/traces` will **not** accept metrics — HTTP metrics go
to `/v1/metrics`, gRPC uses the metrics service. The `otel:destinations` schema and the
downstream receiver both need a metrics path. (This is exactly the BREAK 1/2
endpoint/path/protocol class from the "canyonos serve shows no metrics" incident — do not
assume the span endpoint works for metrics.)

### C. Schema migration gap (`CREATE TABLE IF NOT EXISTS`)
`init_db()` uses `CREATE TABLE IF NOT EXISTS` with no `ALTER`/migration path. Adding
`metrics_waiting` to a **pre-existing** `otel_queue.db` is fine (new table is created), but
any later column addition to `metrics_waiting` will silently never apply on an existing
file and every write then fails. Fix the table shape before shipping, or build a real
migration step now.

### D. Silent wrong-DB-path failure
`sqlite3.connect()` **creates** a missing file instead of raising, so a wrong `DB_PATH`
produces byte-identical logs to a healthy idle exporter. The span side added a
periodic "N rows total / M pending / last export" status line to catch this — the metrics
pass needs the same active check, not event-only logging. A `try/except` cannot catch this.

### E. `sent` means "queued/accepted", not "stored"
On the old `BatchSpanProcessor` design (this branch), `on_end()` only enqueues; a row is
marked sent before the async export is confirmed. Neither HTTP nor gRPC OTLP exporters
parse `partial_success.rejected_data_points`, so a 200/OK with rejected points still marks
sent. Decide the delivery contract for metrics up front (and whether to adopt the newer
synchronous-export design first — item H).

### F. Idempotency / duplicate delivery
All-or-nothing marking re-delivers to already-healthy destinations on a partial failure.
For metrics this means duplicate data points unless the receiver upserts on
`(resource, metric, timestamp)`. Make `sample_id` deterministic (e.g.
`{agent_id}:{observed_at_ns}`) so re-polls upsert into `metrics_waiting` rather than
duplicate, and so re-exports are byte-identical. OTLP guarantees nothing about dedup —
verify per destination.

### G. Unbounded table growth / pruning
`waiting` already never prunes. `metrics_waiting` will grow **much faster** (every instance,
every ~5s), so it needs a prune/TTL policy from day one (e.g. delete `sent = 1` rows older
than N minutes). Without it this table dwarfs the spans table.

### H. Which span-pipeline baseline do we build on?
This branch predates the CAN-334 synchronous-export rewrite (poison-row rejection,
`mark_sent_many`, connectivity probe, empty-queue warning, protocol whitelist). Decide:
build metrics on the older `BatchSpanProcessor` design as-is, or rebase/port the metrics
work onto the hardened span pipeline so both signals share the same delivery guarantees.
Recommendation: align with the hardened design to avoid re-litigating E/F for metrics.

### I. Resource / `service.name`
Spans currently ship with no `Resource`, so they'd show as `service.name=unknown_service`.
Metrics are grouped by resource at every backend, so a proper `Resource`
(`service.name` = agent name, plus `canyon.project.id`, host) matters **more** for metrics
and should be built in from the start, not deferred.

### J. Retire, don't orphan, the Postgres path
`send_agent_information` + `agent_information` become dead once metrics flow through OTel
(this is the CAN-284 plan). Remove them only after the OTel metrics path is verified
end-to-end; keep `pull_runtime_information` (feeds spans). Note `error_count` on the old
table was always 0 (reset before it accumulated) — don't faithfully reproduce that bug.

### K. Per-row isolation on the producer (GC) write
`write_waiting_rows` has no per-row try/except: one malformed row raises before commit and
loses the whole tick. `write_metrics_rows` should isolate per row (or per instance) so one
bad instance's metrics don't drop every instance's metrics for that poll.

### L. Redis topology
The exporter subprocess hardcodes `RedisClient(host="host.docker.internal")`; GC builds its
own from config. Only matters on non-default Redis, and every in-repo config uses
localhost:6379/0. No new plumbing unless a real non-default deployment exists — but the
metrics pass reads the same `otel:destinations` key, so it inherits whatever the span pass uses.

## 8. Open questions (need answers before coding)

1. **Temporality**: delta or cumulative for the three counters? (§7.A)
2. **One subprocess pass or two?** Add `_send_pending_metrics()` to the existing loop, or a
   separate poll cadence for metrics?
3. **Destination config**: reuse each `otel:destinations` entry for both signals (append a
   metrics path), or add a separate metrics endpoint field?
4. **Baseline**: build on this branch's `BatchSpanProcessor` design, or port the hardened
   CAN-334 synchronous-export design first? (§7.H)
5. **Metric names/units**: standard `system.*` semconv vs namespaced `canyonos.*`; percent
   vs 0–1 fraction. (§5)
6. **Prune policy** parameters (TTL, run cadence). (§7.G)
7. **Drop throughput entirely**, or keep it as a derived attribute during transition? (§5)

## 9. Phased execution (each phase independently testable)

- **P0** — Finalize `metrics_waiting` schema + decisions in §8 (this doc, reviewed).
- **P1** — `db.py`: table + `write_metrics_rows` + `mark_metrics_sent` + `init_db` wiring; unit test the writes against a temp DB.
- **P2** — `convert.py`: `metrics_row_to_datapoints` with `Resource`; unit test pure conversion (gauge + sum, deterministic output).
- **P3** — `otel_exporter.py`: metric exporters + `_send_pending_metrics` + shutdown; test with a dead endpoint (rows stay `sent=0`) and a live 200 receiver (`sent=1`), plus no-re-send.
- **P4** — `global_controller.py`: swap `send_agent_information` → `write_metrics_rows`; keep counter drain; drop/keep throughput.
- **P5** — Retire `send_agent_information`/`agent_information`/`_AGENT_UPSERT` and dead imports after E2E verification.
- **P6** — Prune policy + periodic status line + docs (`SCHEMA.md`, `DESIGN.md`).

## 10. Test strategy

- Pure-conversion tests (no I/O), same style as the span converter tests.
- Producer tests: seed a metrics hash + counters in a fake Redis, run the GC drain, assert
  `metrics_waiting` rows and that counters were reset once (and only once) after a successful write.
- Exporter tests: dead endpoint vs live `http.server` returning 200; assert `sent` flags,
  no double-send, partial-failure leaves rows unsent. (Mocked exporters cannot catch
  encode-time failures — include at least one real-encode path.)
- Env caveat (from CAN-334): this checkout's system python lacks deps; use a
  `--system-site-packages` venv + `pip install sqlalchemy redis pyyaml` to run the exporter suites.
