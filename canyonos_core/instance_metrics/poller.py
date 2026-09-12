"""Machine-level instance metrics poller.

Samples CPU, GPU, disk, memory, and uptime on a fixed interval and writes them to this
instance's Redis metrics hash (``controller:{host}:{port}:metrics``). GlobalController
reads that hash on its own poll tick and persists a time-series metrics row.

Best-effort by design: a bad poll tick is logged and skipped, never fatal; the process is
not restarted if it dies (self-healing is a later concern).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time

try:
    from canyonos_core.controller.utils.redis_client import RedisClient
    from canyonos_core.controller.utils.gpu_metrics import read_gpu_metrics
except ImportError:  # standalone in-container layout (flat modules at context root)
    from redis_client import RedisClient
    from gpu_metrics import read_gpu_metrics

try:
    import psutil
except ImportError:  # pragma: no cover - psutil ships in-container; guard for dev import
    psutil = None

logger = logging.getLogger("instance_metrics")

DEFAULT_POLL_INTERVAL = 1.0
# Sleep in small slices between polls so SIGTERM stays responsive.
_SLEEP_SLICE_SECONDS = 0.5

# The collector runs as a container with the host root bind-mounted at /host (see
# GlobalController._launch_metrics_collectors' `-v /:/host:ro`), so path-based disk usage
# is read from there to reflect the real machine rather than the container overlay fs.
# Falls back to "/" when that mount isn't present (e.g. running the poller directly).
# cpu/mem/net/disk-io counters come from host-global /proc via --pid=host/--network=host
# and need no remap; only disk_usage does.
HOST_ROOT = "/host" if os.path.isdir("/host") else "/"


class InstanceMetricsPoller:
    """Polls machine-level metrics and writes them to a Redis metrics hash."""

    def __init__(self, redis_client, metrics_key, interval=DEFAULT_POLL_INTERVAL):
        self.redis = redis_client
        self.metrics_key = metrics_key
        self.interval = interval
        self._running = True
        # Previous cumulative IO counters as (values, timestamp), for computing
        # throughput rates across successive polls. None until the first sample.
        self._prev_disk_io = None
        self._prev_net_io = None
        # Prime cpu_percent so the first real reading isn't 0.0 -- psutil measures
        # utilization between successive calls.
        if psutil is not None:
            psutil.cpu_percent(interval=None)

    def collect(self):
        """Snapshot this machine's metrics.

        This process is one-per-machine, so it only reports machine-level signals plus
        the box's total capacity -- per-agent/per-instance state (queue length, request
        counters, resource requirements) stays in each LocalController's own hash.

        ``observed_at`` is stamped here, at the producer, so a sample's timestamp is its
        measurement time rather than whenever GlobalController later reads the hash.
        Every group is independently guarded so one failing source (e.g. no GPU, no
        /proc/pressure) never drops the rest of the sample.
        """
        sample = {"observed_at": str(time.time())}
        for section in (
            self._cpu, self._memory, self._gpu, self._disk,
            self._network, self._capacity,
        ):
            try:
                sample.update(section())
            except Exception as e:  # best-effort per section
                logger.warning("metrics section %s failed: %s", section.__name__, e)
        return sample

    # -- machine-level sections --------------------------------------------- #

    def _cpu(self):
        utilization = psutil.cpu_percent(interval=None)
        return {
            "cpu_percent": str(utilization),                 # utilization
            "cpu_available_percent": str(max(100.0 - utilization, 0.0)),
            "cpu_pressure": str(self._pressure("cpu")),
        }

    def _memory(self):
        vm = psutil.virtual_memory()
        return {
            "memory_percent": str(vm.percent),
            "memory_used_bytes": str(vm.used),
            "memory_available_bytes": str(vm.available),
            "memory_pressure": str(self._pressure("memory")),
        }

    def _gpu(self):
        utilization, mem_used, mem_free = read_gpu_metrics()
        return {
            "gpu_percent": str(utilization),                 # utilization
            "gpu_memory_used_bytes": str(mem_used),
            "gpu_memory_available_bytes": str(mem_free),
        }

    def _disk(self):
        usage = psutil.disk_usage(HOST_ROOT)
        fields = {
            "disk_percent": str(usage.percent),
            "disk_free_bytes": str(usage.free),
            "uptime_seconds": str(max(time.time() - psutil.boot_time(), 0.0)),
        }
        io = psutil.disk_io_counters()
        read_bps, write_bps = self._io_rate(
            "_prev_disk_io", io,
            ("read_bytes", "write_bytes") if io else None,
        )
        fields["disk_read_bytes_per_sec"] = str(read_bps)
        fields["disk_write_bytes_per_sec"] = str(write_bps)
        return fields

    def _network(self):
        io = psutil.net_io_counters()
        rx_bps, tx_bps = self._io_rate(
            "_prev_net_io", io,
            ("bytes_recv", "bytes_sent") if io else None,
        )
        return {
            "network_rx_bytes_per_sec": str(rx_bps),
            "network_tx_bytes_per_sec": str(tx_bps),
        }

    def _capacity(self):
        """Total physical capacity of this box (machine-level, so it belongs here rather
        than in any single agent's hash)."""
        capacity = {
            "cpu_count": psutil.cpu_count(),
            "memory_total_bytes": psutil.virtual_memory().total,
            "disk_total_bytes": psutil.disk_usage(HOST_ROOT).total,
        }
        return {"machine_capacity": json.dumps(capacity)}

    # -- helpers ------------------------------------------------------------ #

    def _pressure(self, resource):
        """Linux PSI 'some' avg10 for cpu/memory, as a percent. Falls back to a
        load-average proxy for cpu, or 0.0 when PSI is unavailable (e.g. macOS)."""
        try:
            with open(f"/proc/pressure/{resource}") as fh:
                for line in fh:
                    if line.startswith("some"):
                        for token in line.split():
                            if token.startswith("avg10="):
                                return float(token.split("=", 1)[1])
        except (OSError, ValueError):
            pass
        if resource == "cpu":
            try:
                cores = psutil.cpu_count() or 1
                return round(os.getloadavg()[0] / cores * 100.0, 2)
            except (OSError, AttributeError):
                pass
        return 0.0

    def _io_rate(self, prev_attr, counters, field_names):
        """Compute (rate_a, rate_b) bytes/sec for two cumulative counters since the last
        poll. Each counter tracks its own timestamp (disk and network are sampled in the
        same tick, so a shared clock would zero out the second one). Returns (0.0, 0.0)
        on the first sample or when counters are unavailable."""
        now = time.time()
        if counters is None or field_names is None:
            return 0.0, 0.0
        a_field, b_field = field_names
        current = (getattr(counters, a_field), getattr(counters, b_field))
        prev = getattr(self, prev_attr)
        setattr(self, prev_attr, (current, now))
        if prev is None:
            return 0.0, 0.0
        prev_values, prev_time = prev
        elapsed = now - prev_time
        if elapsed <= 0:
            return 0.0, 0.0
        return (
            max(current[0] - prev_values[0], 0) / elapsed,
            max(current[1] - prev_values[1], 0) / elapsed,
        )

    def _write_once(self):
        self.redis.hset_multiple(self.metrics_key, self.collect())

    def stop(self, *_):
        """Signal the run loop to exit (SIGTERM/SIGINT handler)."""
        self._running = False

    def run(self):
        if psutil is None:
            logger.error("psutil is unavailable; instance metrics poller cannot run.")
            return
        logger.info(
            "Instance metrics poller started (key=%s, interval=%.1fs).",
            self.metrics_key,
            self.interval,
        )
        while self._running:
            try:
                self._write_once()
            except Exception as e:  # best-effort: never crash the process on a bad tick
                logger.warning("Instance metrics poll failed (non-fatal): %s", e)
            slept = 0.0
            while self._running and slept < self.interval:
                time.sleep(min(_SLEEP_SLICE_SECONDS, self.interval - slept))
                slept += _SLEEP_SLICE_SECONDS
        logger.info("Instance metrics poller exiting.")


def _metrics_key_from_env():
    """Prefer the key GlobalController hands us; otherwise compose a machine-scoped one.
    This process is one-per-machine, so the key is keyed by host, not by instance."""
    metrics_key = os.environ.get("CANYONOS_METRICS_KEY")
    if metrics_key:
        return metrics_key
    host = os.environ.get("CANYONOS_AGENT_HOST", "localhost")
    return f"machine:{host}:metrics"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redis_host = os.environ.get("CANYONOS_REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("CANYONOS_REDIS_PORT", 6379))
    interval = float(os.environ.get("CANYONOS_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))

    poller = InstanceMetricsPoller(
        RedisClient(host=redis_host, port=redis_port),
        _metrics_key_from_env(),
        interval,
    )
    signal.signal(signal.SIGTERM, poller.stop)
    signal.signal(signal.SIGINT, poller.stop)
    poller.run()


if __name__ == "__main__":
    main()
