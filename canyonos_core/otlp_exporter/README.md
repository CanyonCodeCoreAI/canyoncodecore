# OTLP Exporter for CanyonOS

The OTLP Exporter ships CanyonOS observability to external OTLP-compatible backends. 

Global Controller writes to a local SQLite queue, and a separate exporter process converts unsent rows to OTLP format and delivers them.

## Pipeline

```
Local Controllers  ─►  GlobalController  ─►  SQLite otel_queue.db  ─►  OTLP Exporter subprocess  ─►  external OTLP receiver(s)
                            (single writer)  (traces_waiting + metrics_waiting + logs_waiting)   (single reader)
```

- Global Controller polls each Local Controller's Redis and gets observability data.
- GC then puts this data into a SQLite database with three tables: traces, metrics, and logs.
- A separate OTLP Process polls this SQLite database and pulls the data, performing operations on it to send it to any OTel receiver endpoint.

Note: All GC does is write to the SQLite, nothing else. All OTLP Exporter does is read from the SQLite, except for marking rows "sent", nothing else.

## Core decisions

### Separate Process
The exporter is a separate OS process, spawned by GlobalController.
Rationale: fault isolation from GC's core poll/health loop and independent restart.

### Reloading for new OTel DB Endpoints
Destinations live in the `otel:destinations` Redis key. The exporter re-reads it every poll and
rebuilds its exporters when it changes, so a config reload (SIGHUP) reaches it without a full
restart.

### HTTP/gRPC endpoints
For HTTP, the type of data go to the signal endpoint they correspond with: `/v1/traces`, `/v1/metrics`, `/v1/logs`. gRPC uses the bare endpoint (the
signal is the gRPC service).

### Durable single-table-per-signal queue
Each of the three tables carries a `sent` column (default `0`).
A row is durable until delivered. The implementation is idempotent, a failed batch simply leaves
rows at `sent = 0`, and the next poll retries it.

### Synchronous export
The exporter hand-builds OTLP objects and calls `exporter.export(...)`, which is synchronous and returns a
result code. Can be made to be async later but since this is a separate process from GC I deemed it fine.

### Pruning Data (Selective Deletion of Completed Data)
Every 5 minutes, a function runs that deletes
rows that are delivered (sent = 1), and have a non-null timestamp, and are older than the
signal's retention. If a row has been completed for 30 minutes and somehow not sent, it automatically gets pruned too to prevent bounds from growing too much.

### Flushing Data (Total Annihilation of Data)
The exporter is always running, even with no destinations configured. When
no endpoint is configured it automatically flushes all the data so the tables
don't grow unbounded. As soon as a destination appears (config hot reloaded) it sends data as normal.

## Dependencies
`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`,
`opentelemetry-exporter-otlp-proto-http`.

## Known gaps
- Per-destination delivery state is one `sent` boolean across all destinations, so one
  destination failing re-delivers to the healthy ones (harmless — ids/sample_ids are
  deterministic).
- Never-finished trace rows (`finished_at` never arrives) are never reaped.
- gRPC destinations cannot detect partial rejection (no public seam); HTTP can. (so if a request in a batch errors, we won't know with gRPC)
- Global Controller still responsible for polling (should move to another process or even to this process later)