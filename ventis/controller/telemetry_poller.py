"""Background telemetry collection, on its own thread and timer.

See docs/TELEMETRY_POLLING.md for why this is a separate poller instead of
living inside GlobalController's health-check loop.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ventis.controller.utils.host_utils import container_routing_host
from ventis.controller.utils.telemetry_logging import (
    pull_runtime_information,
    send_agent_information,
    send_runtime_information,
)

logger = logging.getLogger(__name__)


class TelemetryPoller:
    """Persists per-instance runtime and agent telemetry on its own timer.

    The controller only supplies a ``targets_provider`` callable -- it never
    tells the poller when to run. See docs/TELEMETRY_POLLING.md.
    """

    def __init__(self, targets_provider, poll_interval=5, database_url=""):
        self._targets_provider = targets_provider
        self._lock = threading.Lock()
        self._poll_interval = poll_interval
        self._database_url = database_url
        self._stop_event = threading.Event()
        self._thread = None
        self._last_metrics_poll_time = {}

    def start(self):
        """Start polling in a daemon thread, if it is not already running.

        Returns ``True`` when a new thread was started and ``False`` when the
        existing thread is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="ventis-telemetry-poller", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout=5):
        """Request shutdown and return whether the polling thread stopped."""
        self._stop_event.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def update_settings(self, poll_interval, database_url):
        """Atomically replace the interval and database URL for subsequent cycles."""
        with self._lock:
            self._poll_interval = poll_interval
            self._database_url = database_url

    def _run(self):
        """Poll on its own timer, independent of whatever else the controller is doing."""
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                logger.warning("Telemetry polling cycle encountered an error: %s", exc)
            with self._lock:
                interval = self._poll_interval
            if self._stop_event.wait(interval):
                return

    def _poll_once(self):
        """Poll every currently known target once, in parallel."""
        targets = tuple(self._targets_provider())
        if not targets:
            return
        with self._lock:
            database_url = self._database_url
        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            list(
                executor.map(
                    lambda target: self._poll_target_safely(target, database_url),
                    targets,
                )
            )

    def _poll_target_safely(self, target, database_url):
        """Keep an unexpected Redis failure isolated to one instance."""
        instance, node_redis = target
        try:
            self._poll_instance(instance, node_redis, database_url)
        except Exception as exc:
            logger.warning(
                "Failed to poll telemetry for instance %s (%s:%s) (non-fatal): %s",
                instance.get("agent_name", "(unknown)"),
                instance.get("host", "(unknown)"),
                instance.get("host_port", "(unknown)"),
                exc,
            )

    def _poll_instance(self, instance, node_redis, database_url):
        """Persist runtime and agent telemetry for one instance."""
        name = instance["agent_name"]
        host = instance["host"]
        port = instance["host_port"]
        try:
            send_runtime_information(
                pull_runtime_information(node_redis), node_redis, database_url
            )
        except Exception as exc:
            logger.warning(
                "Failed to write runtime information for instance %s (%s:%s) "
                "(non-fatal): %s",
                name,
                host,
                port,
                exc,
            )

        metrics_key = f"controller:{container_routing_host(host)}:{port}:metrics"
        metrics = node_redis.hgetall(metrics_key)
        if not metrics:
            return

        now = time.time()
        requests_served = int(float(metrics.get("requests_served") or 0))
        elapsed = now - self._last_metrics_poll_time.get(
            (host, port), now - self._poll_interval
        )
        throughput = requests_served / elapsed if elapsed > 0 else 0.0
        self._last_metrics_poll_time[(host, port)] = now

        try:
            send_agent_information(
                [
                    {
                        **instance,
                        **metrics,
                        "requests_served": requests_served,
                        "throughput": throughput,
                    }
                ],
                database_url,
            )
        except Exception as exc:
            logger.warning(
                "Failed to write agent information for instance %s (%s:%s) "
                "(non-fatal): %s",
                name,
                host,
                port,
                exc,
            )
        else:
            # Drain counters only after the corresponding row has persisted.
            node_redis.hset_multiple(
                metrics_key,
                {
                    "full_failures": 0,
                    "error_count": 0,
                    "requests_served": 0,
                },
            )
