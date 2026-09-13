# Metrics

The metrics signal emits two kinds of samples: 

- **machine** (per-machine/instance resource usage)
- **agent** (per-agent/container metrics).

Both go through the same `metrics_waiting` table and the shared exporter loop. This doc covers only the metric-specific parts.

## Flow

```
producers:
  (machine)  metrics collector container  ─►  Redis machine:{host}:metrics
  (agent)    LocalController._collect_metrics + _execute_locally  ─►  Redis controller:{host}:{port}:metrics
  ─►  GC polls all metrics and puts them into a SQLite `metrics_waiting` table (GC does NO per-metric logic)
  ─►  OTLP Exporter reads rows from this table, converts data, and sends it to external OTLP receiver
```

## Producers

### Machine collector (per machine)
A per-machine sibling container launched by GC sees machine CPU/mem/GPU/disk/network. It runs, stamps its own `observed_at`, and writes the hash
`machine:{host}:metrics` into Redis. 

Fields:

`cpu_percent`, `cpu_available_percent`, `cpu_pressure`, `memory_percent`,
`memory_used_bytes`, `memory_available_bytes`, `memory_pressure`, `gpu_percent`,
`gpu_memory_used_bytes`, `gpu_memory_available_bytes`, `disk_percent`, `disk_free_bytes`,
`disk_read_bytes_per_sec`, `disk_write_bytes_per_sec`, `network_rx_bytes_per_sec`,
`network_tx_bytes_per_sec`, `uptime_seconds`, `machine_capacity` (JSON: `cpu_count`,
`memory_total_bytes`, `disk_total_bytes`), `observed_at`.

### Agent metrics (per local controller)
`LocalController` publishes `controller:{host}:{port}:metrics`.

Fields: `status`, `queue_length`, `observed_at`, `started_at`, `requests_served` and
`full_failures`.

## `metrics_waiting` table schema (`db.py`)

One row per sample. The `kind` variable discriminates machine vs agent; identity columns are queryable
and NULL where a kind doesn't use them.

| Column | Meaning |
| --- | --- |
| `sample_id` (PK) | `{kind}:{agent_id or host[:port]}:{observed_at_ns}` — deterministic, so a GC re-poll of the same tick upserts instead of duplicating. |
| `kind` | `machine` or `agent`. |
| `agent_id`, `agent_name` | Agent identity (agent rows). |
| `host`, `port` | Location; `port` NULL for machine rows. |
| `project_id` | Deployment/project id. |
| `observed_at` | Producer-stamped timestamp (unix seconds); both machine and agent samples stamp `observed_at`. |
| `metrics` | **THIS HAS THE MAJORITY OF THE METRICS, EVERY OTHER COLUMN IS METADATA. It is JSON so we can freely have different schemas, as shown below with machine/agent.** . |
| `sent` | Send-tracking, default `0`; set `1` only after delivery to every destination. |

### Machine (`kind = machine`)
Resource: `service.name = canyonos-machine-{host}`

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

### Agent (`kind = agent`)
Resource: `service.name = agent_name`, `service.instance.id = agent_id` (standard OTel
semconv, kept as-is), `host.name`, `canyonos.agent.port`, `canyonos.project.id`.

| Field | Metric | Instrument | Unit |
| --- | --- | --- | --- |
| `queue_length` | `canyonos.agent.queue.length` | Gauge | `{item}` |
| `requests_served` | `canyonos.agent.requests` | Sum (monotonic, cumulative) | `{request}` |
| `full_failures` | `canyonos.agent.failures` | Sum (monotonic, cumulative) | `{failure}` |
| `status` | `canyonos.agent.up` | Gauge (`1` if `healthy` else `0`) | `1` |

## Endpoint
HTTP metric exporters target `<endpoint>/v1/metrics`; gRPC uses
the bare endpoint. The configured destination `endpoint` is the OTLP root, shared with the
trace signal, which appends `/v1/traces`.
