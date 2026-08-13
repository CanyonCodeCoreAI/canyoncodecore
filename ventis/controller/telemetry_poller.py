"""Background telemetry collection for local controller instances.

``TelemetryPoller`` deliberately has no dependency on ``GlobalController`` or
``InstanceManager``. The controller submits plain ``(instance, redis_client)``
targets for each cycle, keeping discovery and infrastructure ownership outside
telemetry.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ventis.controller.utils.telemetry_logging import (
    pull_runtime_information,
    send_agent_information,
    send_runtime_information,
)


logger = logging.getLogger(__name__)


def _container_host(host):
    """Return the address a Dockerized local controller uses in Redis keys."""
    return "host.docker.internal" if host in {"localhost", "127.0.0.1"} else host


class TelemetryPoller:
    """Persist per-instance runtime and agent telemetry on a fixed cadence.

    The controller supplies fully resolved polling targets through
    ``request_poll()`` or ``poll_once()``. The poller never discovers instances,
    resolves infrastructure, or retains an authoritative instance catalogue.
    """

    def __init__(
        self,
        poll_interval=5,
        database_url="",
    ):
        self._settings_lock = threading.Lock()
        self._poll_interval = poll_interval
        self._database_url = database_url

        self._cycle_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._request_condition = threading.Condition()
        self._pending_targets = None
        self._thread = None
        self._last_metrics_poll_time = {}

    def start(self):
        """Start polling in a daemon thread, if it is not already running.

        Returns ``True`` when a new thread was started and ``False`` when the
        existing thread is already running.
        """
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            with self._request_condition:
                self._pending_targets = None
            self._thread = threading.Thread(
                target=self._run,
                name="ventis-telemetry-poller",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout=5):
        """Request shutdown and return whether the polling thread stopped."""
        self._stop_event.set()
        with self._lifecycle_lock:
            thread = self._thread
        with self._request_condition:
            self._request_condition.notify_all()

        if thread is None or not thread.is_alive():
            return True
        if thread is threading.current_thread():
            return False

        thread.join(timeout=timeout)
        return not thread.is_alive()

    def update_settings(self, poll_interval, database_url):
        """Atomically replace the interval and database URL for subsequent cycles."""
        with self._settings_lock:
            self._poll_interval = poll_interval
            self._database_url = database_url

    def request_poll(self, targets):
        """Queue controller-resolved targets for background polling.

        Only one pending snapshot is retained. If persistence is blocked, newer
        controller snapshots replace older pending work instead of building an
        unbounded backlog.
        """
        with self._request_condition:
            self._pending_targets = tuple(targets)
            self._request_condition.notify()

    def poll_once(self, targets):
        """Synchronously poll controller-resolved targets.

        A cycle already in progress is left alone; this keeps a slow database
        write from overlapping with a timer-triggered or manually-triggered
        poll.  ``False`` indicates that the call was skipped for that reason.
        """
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Telemetry polling cycle already in progress; skipping overlap.")
            return False

        try:
            with self._settings_lock:
                poll_interval = self._poll_interval
                database_url = self._database_url
            targets = tuple(targets)

            if not targets:
                return True

            with ThreadPoolExecutor(max_workers=len(targets)) as executor:
                list(
                    executor.map(
                        lambda target: self._poll_target_safely(
                            target, poll_interval, database_url
                        ),
                        targets,
                    )
                )
            return True
        finally:
            self._cycle_lock.release()

    def _run(self):
        """Process controller-submitted cycles without blocking its health loop."""
        while True:
            with self._request_condition:
                while (
                    self._pending_targets is None
                    and not self._stop_event.is_set()
                ):
                    self._request_condition.wait()
                if self._stop_event.is_set():
                    return
                targets = self._pending_targets
                self._pending_targets = None

            try:
                self.poll_once(targets)
            except Exception as exc:
                logger.warning("Telemetry polling loop encountered an error: %s", exc)

    def _poll_target_safely(self, target, poll_interval, database_url):
        """Keep an unexpected Redis failure isolated to one instance."""
        instance, node_redis = target
        try:
            self._poll_instance(
                instance, node_redis, poll_interval, database_url
            )
        except Exception as exc:
            logger.warning(
                "Failed to poll telemetry for instance %s (%s:%s) (non-fatal): %s",
                instance.get("agent_name", "(unknown)"),
                instance.get("host", "(unknown)"),
                instance.get("host_port", "(unknown)"),
                exc,
            )

    def _poll_instance(self, instance, node_redis, poll_interval, database_url):
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

        container_host = _container_host(host)
        metrics_key = f"controller:{container_host}:{port}:metrics"
        metrics = node_redis.hgetall(metrics_key)
        if not metrics:
            return

        now = time.time()
        requests_served = int(float(metrics.get("requests_served") or 0))
        elapsed = now - self._last_metrics_poll_time.get(
            (host, port), now - poll_interval
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
