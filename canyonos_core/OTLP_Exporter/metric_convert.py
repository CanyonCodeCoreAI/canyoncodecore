"""Converts a ``metrics_waiting`` row into OTel metric data.

A row is one sample from a Redis metrics hash, discriminated by ``kind``:
  * ``machine`` -- a per-host sample from the metrics collector (all gauges);
  * ``instance`` -- a per-replica sample from a LocalController (queue-depth gauge, a
    health gauge, and the request counters as monotonic cumulative Sums).
The exporter collects one ResourceMetrics per row into a single MetricsData and exports
the batch at once, mirroring how the span path batches a poll's worth of spans.

Trace attribution (session_id→trace_id, future_id→span_id) does not apply: metrics are not
tied to a single execution, so unlike span_convert/log_convert this converter uses only the
shared ``to_epoch_nanos`` helper from otlp_utils.

Pure function, no I/O.
"""

import json

from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Gauge,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from otlp_utils import to_epoch_nanos

_SCOPE = InstrumentationScope("canyonos.instance_metrics")

# metric field -> (OTel metric name, unit). Namespaced canyonos.machine.* per the OTel
# naming spec's app-prefix rule (no stable semconv covers most of these). All gauges.
_MACHINE_GAUGES = {
    "cpu_percent": ("canyonos.machine.cpu.utilization", "%"),
    "cpu_available_percent": ("canyonos.machine.cpu.available", "%"),
    "cpu_pressure": ("canyonos.machine.cpu.pressure", "1"),
    "memory_percent": ("canyonos.machine.memory.utilization", "%"),
    "memory_used_bytes": ("canyonos.machine.memory.used", "By"),
    "memory_available_bytes": ("canyonos.machine.memory.available", "By"),
    "memory_pressure": ("canyonos.machine.memory.pressure", "1"),
    "gpu_percent": ("canyonos.machine.gpu.utilization", "%"),
    "gpu_memory_used_bytes": ("canyonos.machine.gpu.memory.used", "By"),
    "gpu_memory_available_bytes": ("canyonos.machine.gpu.memory.available", "By"),
    "disk_percent": ("canyonos.machine.disk.utilization", "%"),
    "disk_free_bytes": ("canyonos.machine.disk.free", "By"),
    "disk_read_bytes_per_sec": ("canyonos.machine.disk.read", "By/s"),
    "disk_write_bytes_per_sec": ("canyonos.machine.disk.write", "By/s"),
    "network_rx_bytes_per_sec": ("canyonos.machine.network.rx", "By/s"),
    "network_tx_bytes_per_sec": ("canyonos.machine.network.tx", "By/s"),
    "uptime_seconds": ("canyonos.machine.uptime", "s"),
}
# machine_capacity is a nested JSON object; its fields are gauges too.
_CAPACITY_GAUGES = {
    "cpu_count": ("canyonos.machine.cpu.count", "{cpu}"),
    "memory_total_bytes": ("canyonos.machine.memory.total", "By"),
    "disk_total_bytes": ("canyonos.machine.disk.total", "By"),
}

# Per-instance (per-replica) metrics. queue_length is a point-in-time gauge; the request
# counters are cumulative monotonic Sums (LocalController never resets them, so each
# instance lifetime is one series and a restart is a natural reset via a new started_at).
_INSTANCE_GAUGES = {
    "queue_length": ("canyonos.instance.queue.length", "{item}"),
}
_INSTANCE_SUMS = {
    "requests_served": ("canyonos.instance.requests", "{request}"),
    "full_failures": ("canyonos.instance.failures", "{failure}"),
}


def _coerce_number(raw):
    """Return an int/float for a stored string value, or None if not numeric."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def _gauge(name, unit, value, time_nanos, attributes):
    return Metric(
        name=name,
        description="",
        unit=unit,
        data=Gauge(
            data_points=[
                NumberDataPoint(
                    attributes=attributes,
                    start_time_unix_nano=time_nanos,
                    time_unix_nano=time_nanos,
                    value=value,
                )
            ]
        ),
    )


def _sum(name, unit, value, start_nanos, time_nanos, attributes):
    return Metric(
        name=name,
        description="",
        unit=unit,
        data=Sum(
            data_points=[
                NumberDataPoint(
                    attributes=attributes,
                    start_time_unix_nano=start_nanos,
                    time_unix_nano=time_nanos,
                    value=value,
                )
            ],
            aggregation_temporality=AggregationTemporality.CUMULATIVE,
            is_monotonic=True,
        ),
    )


def metrics_row_to_resource_metrics(row):
    """Convert one ``metrics_waiting`` row into a ResourceMetrics, or None.

    Dispatches on ``kind`` (machine vs instance). Returns None when the row has no
    parseable metrics, so the exporter can mark such a sample done without emitting it.
    """
    row = dict(row)
    if (row.get("kind") or "machine") == "instance": # "or "machine"" so no errors
        return _instance_resource_metrics(row)
    return _machine_resource_metrics(row)


def _instance_resource_metrics(row):
    """Build a ResourceMetrics for a per-replica instance sample."""
    try:
        values = json.loads(row.get("metrics") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not values:
        return None

    time_nanos = to_epoch_nanos(row.get("observed_at"))
    # Cumulative Sums start at the instance's start; a new started_at (restart) is a reset.
    start_nanos = to_epoch_nanos(values.get("started_at")) or time_nanos
    point_attributes = {}

    metrics = []
    for field, (name, unit) in _INSTANCE_GAUGES.items():
        value = _coerce_number(values.get(field))
        if value is not None:
            metrics.append(_gauge(name, unit, value, time_nanos, point_attributes))
    for field, (name, unit) in _INSTANCE_SUMS.items():
        value = _coerce_number(values.get(field))
        if value is not None:
            metrics.append(
                _sum(name, unit, value, start_nanos, time_nanos, point_attributes)
            )
    status = values.get("status")
    if status is not None:
        metrics.append(
            _gauge(
                "canyonos.instance.up",
                "1",
                1 if status == "healthy" else 0,
                time_nanos,
                point_attributes,
            )
        )

    if not metrics:
        return None

    resource_attributes = {
        "service.name": row.get("agent_name") or row.get("agent_id") or "canyonos-instance"
    }
    if row.get("agent_id"):
        resource_attributes["service.instance.id"] = row["agent_id"]
    if row.get("host"):
        resource_attributes["host.name"] = row["host"]
    if row.get("port"):
        resource_attributes["canyonos.instance.port"] = row["port"]
    if row.get("project_id"):
        resource_attributes["canyonos.project.id"] = row["project_id"]

    return ResourceMetrics(
        resource=Resource.create(resource_attributes),
        scope_metrics=[ScopeMetrics(scope=_SCOPE, metrics=metrics, schema_url="")],
        schema_url="",
    )


def _machine_resource_metrics(row):
    """Build a ResourceMetrics for a per-host machine sample (all gauges)."""
    try:
        values = json.loads(row.get("metrics") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not values:
        return None

    time_nanos = to_epoch_nanos(row.get("observed_at"))
    host = row.get("host")
    point_attributes = {"host.name": host} if host else {}

    metrics = []
    for field, (name, unit) in _MACHINE_GAUGES.items():
        value = _coerce_number(values.get(field))
        if value is not None:
            metrics.append(_gauge(name, unit, value, time_nanos, point_attributes))

    # machine_capacity is itself a JSON blob; expand it into its own gauges.
    capacity_raw = values.get("machine_capacity")
    if capacity_raw:
        try:
            capacity = json.loads(capacity_raw)
        except (json.JSONDecodeError, TypeError):
            capacity = {}
        for field, (name, unit) in _CAPACITY_GAUGES.items():
            value = _coerce_number(capacity.get(field))
            if value is not None:
                metrics.append(_gauge(name, unit, value, time_nanos, point_attributes))

    if not metrics:
        return None

    # host/project identify the sender (resource, not data point); project_id is canyonos-namespaced since OTel has no standard field for it.
    resource_attributes = {"service.name": f"canyonos-machine-{host}" if host else "canyonos-machine"}
    if host:
        resource_attributes["host.name"] = host
    if row.get("project_id"):
        resource_attributes["canyonos.project.id"] = row["project_id"]

    return ResourceMetrics(
        resource=Resource.create(resource_attributes),
        scope_metrics=[ScopeMetrics(scope=_SCOPE, metrics=metrics, schema_url="")],
        schema_url="",
    )
