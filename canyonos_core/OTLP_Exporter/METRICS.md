# Metrics — the `metrics_waiting` table and sample→metric conversion

The metrics signal emits two kinds of retrospective, poll-driven samples: **machine**
(per-host resource usage) and **instance** (per-replica app metrics). Both flow through the
same `metrics_waiting` table and the shared exporter loop. See [DESIGN.md](./DESIGN.md) for
the common pipeline; this doc covers only the metric-specific parts.

## Flow

```
producers:
  (machine)  metrics collector container  ─►  Redis machine:{host}:metrics
  (instance) LocalController._collect_metrics + _execute_locally  ─►  Redis controller:{host}:{port}:metrics
  ─►  GC reads each hash and hands it verbatim to db.metric_write_rows  (GC does NO per-metric logic)
  ─►  SQLite `metrics_waiting` table
  ─►  OTLP Exporter: _metric_send_pending() reads unsent rows
        (metric_convert.metric_row_to_resource_metrics → OTLPMetricExporter.export)
  ─►  external OTLP receiver
```

GC is a **dumb poller**: `_poll_machine_metrics` reads `machine:{host}:metrics` once per
unique host, and `_poll_one_instance` reads `controller:{host}:{port}:metrics` per instance.
Each is written verbatim (as a `kind`-tagged row) — all interpretation happens later in
`metric_convert`.

## Producers

### Machine collector (per host)
A per-machine sibling container launched by GC (`_launch_metrics_collectors`), run with
`--pid=host --network=host -v /:/host:ro` (and `--gpus all` when the host config declares a
GPU) so it can see host CPU/mem/GPU/disk/network. It runs `python -m
canyonos_core.instance_metrics`, stamps its own `observed_at`, and writes the hash
`machine:{host}:metrics`. Fields:

`cpu_percent`, `cpu_available_percent`, `cpu_pressure`, `memory_percent`,
`memory_used_bytes`, `memory_available_bytes`, `memory_pressure`, `gpu_percent`,
`gpu_memory_used_bytes`, `gpu_memory_available_bytes`, `disk_percent`, `disk_free_bytes`,
`disk_read_bytes_per_sec`, `disk_write_bytes_per_sec`, `network_rx_bytes_per_sec`,
`network_tx_bytes_per_sec`, `uptime_seconds`, `machine_capacity` (JSON: `cpu_count`,
`memory_total_bytes`, `disk_total_bytes`), `observed_at`.

Throughput fields are `0.0` on the first tick (no prior sample to delta against).

### Instance metrics (per replica)
`LocalController` publishes `controller:{host}:{port}:metrics` and, at startup, resets the
cumulative counters and stamps a start time. Fields: `status`, `queue_length`, `observed_at`
(the producer timestamp), `started_at`, plus the cumulative counters `requests_served` and
`full_failures` (incremented via `hincrby` in `_execute_locally`, **never reset per poll**).

## `metrics_waiting` table schema (`db.py`)

One row per sample. `kind` discriminates machine vs instance; identity columns are queryable
and NULL where a kind doesn't use them.

| Column | Meaning |
| --- | --- |
| `sample_id` (PK) | `{kind}:{agent_id or host[:port]}:{observed_at_ns}` — deterministic, so a GC re-poll of the same tick upserts instead of duplicating. |
| `kind` | `machine` or `instance`. |
| `agent_id`, `agent_name` | Instance identity (instance rows). |
| `host`, `port` | Location; `port` NULL for machine rows. |
| `project_id` | Deployment/project id. |
| `observed_at` | Producer-stamped timestamp (unix seconds); both machine and instance samples stamp `observed_at`. |
| `metrics` | The full producer hash as a JSON blob (schema-additive — new fields need no `ALTER`). |
| `sent` | Send-tracking, default `0`; set `1` only after delivery to every destination. |

Read query: `SELECT * FROM metrics_waiting WHERE sent IS NULL OR sent = 0 LIMIT
MAX_SPANS_PER_POLL`.

## Sample → metric conversion (`metric_convert.py`)

`metric_row_to_resource_metrics(row)` dispatches on `kind`, returning one `ResourceMetrics`
(the exporter batches all rows into one `MetricsData`). A row whose `metrics` blob is empty
or unparseable returns `None` and is marked sent (retired), not retried forever.

Scope: `canyonos.instance_metrics`. Names are namespaced `canyonos.*` (no stable semconv
covers most of these).

### Machine (`kind = machine`) — all gauges
Resource: `service.name = canyonos-machine-{host}`, `host.name`, `canyonos.project.id`.

| Field | Metric | Unit |
| --- | --- | --- |
| `cpu_percent` | `canyonos.machine.cpu.utilization` | `%` |
| `cpu_available_percent` | `canyonos.machine.cpu.available` | `%` |
| `cpu_pressure` | `canyonos.machine.cpu.pressure` | `1` |
| `memory_percent` | `canyonos.machine.memory.utilization` | `%` |
| `memory_used_bytes` | `canyonos.machine.memory.used` | `By` |
| `memory_available_bytes` | `canyonos.machine.memory.available` | `By` |
| `memory_pressure` | `canyonos.machine.memory.pressure` | `1` |
| `gpu_percent` | `canyonos.machine.gpu.utilization` | `%` |
| `gpu_memory_used_bytes` | `canyonos.machine.gpu.memory.used` | `By` |
| `gpu_memory_available_bytes` | `canyonos.machine.gpu.memory.available` | `By` |
| `disk_percent` | `canyonos.machine.disk.utilization` | `%` |
| `disk_free_bytes` | `canyonos.machine.disk.free` | `By` |
| `disk_read_bytes_per_sec` | `canyonos.machine.disk.read` | `By/s` |
| `disk_write_bytes_per_sec` | `canyonos.machine.disk.write` | `By/s` |
| `network_rx_bytes_per_sec` | `canyonos.machine.network.rx` | `By/s` |
| `network_tx_bytes_per_sec` | `canyonos.machine.network.tx` | `By/s` |
| `uptime_seconds` | `canyonos.machine.uptime` | `s` |
| `machine_capacity.cpu_count` | `canyonos.machine.cpu.count` | `{cpu}` |
| `machine_capacity.memory_total_bytes` | `canyonos.machine.memory.total` | `By` |
| `machine_capacity.disk_total_bytes` | `canyonos.machine.disk.total` | `By` |

### Instance (`kind = instance`)
Resource: `service.name = agent_name`, `service.instance.id = agent_id`, `host.name`,
`canyonos.instance.port`, `canyonos.project.id`.

| Field | Metric | Instrument | Unit |
| --- | --- | --- | --- |
| `queue_length` | `canyonos.instance.queue.length` | Gauge | `{item}` |
| `requests_served` | `canyonos.instance.requests` | Sum (monotonic, cumulative) | `{request}` |
| `full_failures` | `canyonos.instance.failures` | Sum (monotonic, cumulative) | `{failure}` |
| `status` | `canyonos.instance.up` | Gauge (`1` if `healthy` else `0`) | `1` |

**Counter semantics:** the counters are cumulative monotonic `Sum`s. `LocalController`
resets them to 0 and stamps `started_at` once at startup, so each instance lifetime is one
clean cumulative series; the Sum's `start_time_unix_nano` is `started_at` and a restart is a
natural counter reset OTel handles via the changed start time. GC never resets them per poll,
so `error_count` — which was never actually incremented — was dropped rather than faithfully
reproduced as a perpetual 0.

## Delivery specifics
- Whole batch exported per destination via synchronous `exporter.export(MetricsData)` →
  `MetricExportResult`. No per-datapoint partial-success recorder (the OTLP metrics response
  shape differs; per-datapoint rejection detection is a later refinement — a batch is
  accepted or retried whole).
- Marking is all-or-nothing across destinations (shared `_deliver_and_mark`); deterministic
  `sample_id`s collapse any re-delivery duplicates at the backend.
- **Empty-queue warning**: after `EMPTY_QUEUE_WARNING_POLLS` empty polls, if
  `metrics_waiting` is genuinely empty, warn once (`_metric_note_queue_state`) — tracked
  independently from traces.
- **Retention**: sent rows older than `METRIC_RETENTION_SECONDS` (10 min, by `observed_at`)
  are pruned (shorter than traces because metrics accrue every poll for every instance and
  host); unsent/recent/null-timestamp rows are never removed.

## Endpoint
HTTP metric exporters target `<endpoint>/v1/metrics`; gRPC uses
the bare endpoint. The configured destination `endpoint` is the OTLP root, shared with the
trace signal, which appends `/v1/traces`.
