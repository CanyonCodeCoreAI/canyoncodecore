# Machine Metrics Poller

This process is responsible for gathering machine-level information about the device it is running on.
This process will write the data to the machine:{host}:metrics field in the Redis container on the machine, which will get polled by the Global Controller and pushed into the `metrics` table of the OTLP Exporter process.

Below is the data that this process collects:

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
