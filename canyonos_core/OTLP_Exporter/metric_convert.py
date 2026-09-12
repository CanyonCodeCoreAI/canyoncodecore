"""Converts a ``metrics_waiting`` row into OTel metric data.

Each row is one machine-level sample (see canyonos_core.instance_metrics.poller) with the
gauge values in a JSON ``metrics`` blob and a producer-stamped ``observed_at``. This builds
a single MetricsData carrying one Gauge per metric under a Resource that identifies the
host. Machine metrics are all gauges -- the request counters are per-instance and live
elsewhere -- so there is no temporality/monotonic decision to make here.

Trace attribution (session_id→trace_id, future_id→span_id) does not apply: metrics are not
tied to a single execution, so unlike span_convert/log_convert this converter uses only the
shared ``to_epoch_nanos`` helper from otlp_utils.

Pure function, no I/O.
"""

import json

from opentelemetry.sdk.metrics.export import (
    Gauge,
    Metric,
    MetricsData,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from otlp_utils import to_epoch_nanos

_SCOPE = InstrumentationScope("canyonos.instance_metrics")

# metric field -> (OTel metric name, unit). Namespaced canyonos.machine.* per the OTel
# naming spec's app-prefix rule (no stable semconv covers most of these). All gauges.
_GAUGES = {
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


def metrics_row_to_metrics_data(row):
    """Convert one ``metrics_waiting`` row into a list holding a single MetricsData.

    Returns an empty list when the row has no parseable metrics (so the exporter's
    generic per-signal loop can treat it uniformly with spans/logs -- a list of items
    to emit, and an empty list means "nothing to send, just mark done").
    """
    row = dict(row)
    try:
        values = json.loads(row.get("metrics") or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    if not values:
        return []

    time_nanos = to_epoch_nanos(row.get("observed_at"))
    host = row.get("host")
    point_attributes = {"host.name": host} if host else {}

    metrics = []
    for field, (name, unit) in _GAUGES.items():
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
        return []

    # Host/project are resource attributes (they identify the producing machine),
    # canyonos.*-namespaced for the Ventis-specific one per the OTel naming spec.
    resource_attributes = {"service.name": f"canyonos-machine-{host}" if host else "canyonos-machine"}
    if host:
        resource_attributes["host.name"] = host
    if row.get("project_id"):
        resource_attributes["canyonos.project.id"] = row["project_id"]

    return [
        MetricsData(
            resource_metrics=[
                ResourceMetrics(
                    resource=Resource.create(resource_attributes),
                    scope_metrics=[
                        ScopeMetrics(scope=_SCOPE, metrics=metrics, schema_url="")
                    ],
                    schema_url="",
                )
            ]
        )
    ]
