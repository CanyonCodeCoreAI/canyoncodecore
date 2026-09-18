# Reconciler
# Separate process that converges running instances onto the desired replica
# counts in Redis, and replaces instances that stop answering.
#
# Level-triggered: every pass recomputes what should exist from the desired state
# and what does exist from the instance records, so a lost wake signal or a crash
# mid-pass costs latency, never correctness.

import argparse
import logging
import os
import signal
import socket
import sys
import time

from canyonos_core.controller.controller_context import ControllerContext
from canyonos_core.controller.instance_manager import InstanceManager
from canyonos_core.reconciler import state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Wake-queue block: bounds how long shutdown waits, not the sweep cadence.
WAKE_TIMEOUT_SECONDS = 1

TCP_PROBE_TIMEOUT_SECONDS = 2

_running = True


def _handle_shutdown(signum, frame):
    global _running
    _running = False


class Reconciler(object):
    """Converges observed instances onto the desired replica counts in Redis."""

    def __init__(self, config_path, sweep_interval=None):
        self.context = ControllerContext(config_path)
        self.context.attach_local_node_redis()
        self.instance_manager = InstanceManager(self.context)
        self.sweep_interval = sweep_interval or self.context.poll_interval

        # A replica reports in on the same cadence GlobalController polls with, so
        # allow a couple of missed reports before calling it dead.
        self.stale_after = 3 * self.context.poll_interval

        # An instance that has never reported yet is still starting, not unhealthy;
        # without this the reaper would destroy and recreate it forever.
        self.startup_grace = max(30, 3 * self.context.poll_interval)

        self._seen_healthy = set()  # instance_id, once it has answered at least once

    # ------------------------------------------------------------------ #
    #  Health                                                            #
    # ------------------------------------------------------------------ #

    def _accepts_connections(self, instance):
        """Whether the instance's gRPC endpoint is reachable from this host."""
        host = instance.get("host")
        port = instance.get("host_port")
        if not host or not port:
            return False
        try:
            with socket.create_connection(
                (host, int(port)), timeout=TCP_PROBE_TIMEOUT_SECONDS
            ):
                return True
        except (OSError, ValueError):
            return False

    def _reports_are_fresh(self, instance):
        """
        Whether the instance's metrics heartbeat is recent.

        Catches a replica whose gRPC server still accepts connections while its
        agent has stopped making progress, which a connection probe cannot see.
        """
        node_redis = self.context.node_redis_for_instance(instance)
        agent_host = self.context._agent_host_key(instance["host"])
        key = f"controller:{agent_host}:{instance['host_port']}:metrics"
        try:
            metrics = node_redis.hgetall(key)
        except Exception as e:
            logger.warning("Failed to read metrics for %s: %s", key, e)
            return True  # a Redis blip is not evidence the instance is unhealthy

        updated_at = metrics.get("updated_at")
        if not updated_at:
            return False
        try:
            return time.time() - float(updated_at) <= self.stale_after
        except (TypeError, ValueError):
            return False

    def _is_healthy(self, instance, instance_id):
        if self._accepts_connections(instance) and self._reports_are_fresh(instance):
            self._seen_healthy.add(instance_id)
            return True
        if instance_id not in self._seen_healthy and self._within_startup_grace(
            instance
        ):
            return True
        return False

    def _within_startup_grace(self, instance):
        created_at = instance.get("created_at")
        if not created_at:
            return False
        try:
            return time.time() - float(created_at) <= self.startup_grace
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------------ #
    #  Reconcile                                                         #
    # ------------------------------------------------------------------ #

    def reconcile(self, agent_name):
        """Make the instances of one agent match its desired replica count."""
        if self._reap(agent_name):
            self._fill()

    def _reap(self, agent_name):
        """Remove an agent's surplus, unhealthy and replaced instances."""
        spec = self.context.agent_specs.get(agent_name)
        if spec is None:
            logger.warning(
                "Wake signal for unknown agent %s; it is not in %s",
                agent_name,
                self.context.config_path,
            )
            return False

        configured = spec.get("replicas", 1)
        if not isinstance(configured, int):
            logger.warning(
                "Agent %s declares a non-integer replicas value (%r); "
                "reconciliation needs a count.",
                agent_name,
                configured,
            )
            return False

        redis_client = self.context.redis
        desired = state.get_desired(redis_client, agent_name, configured)
        reap_requested = state.take_reap_requests(redis_client, agent_name)

        instances = self.instance_manager.list_instances(agent_name)
        for instance in instances:
            # Routing republishes fan out to node_redis, so every node holding an
            # instance needs a client before one is removed.
            self.context.node_redis_for_instance(instance)

        for instance in instances:
            instance_id = self.instance_manager._instance_id_from_record(instance)
            reason = self._removal_reason(
                instance, instance_id, desired, reap_requested
            )
            if reason is None:
                continue
            logger.info("Removing instance %s (%s)", instance_id, reason)
            self._seen_healthy.discard(instance_id)
            self.instance_manager.remove_instance(instance_id)
        return True

    def _fill(self):
        """
        Provision whatever is missing across every agent at once.

        ensure_instances is create-only and per-slot idempotent, and it takes the
        whole spec list because it republishes the routing snapshot from what it is
        handed -- so this runs once per pass rather than once per agent.
        """
        self.instance_manager.ensure_instances(
            state.desired_agent_specs(self.context.redis, self.context.controllers)
        )

    def _removal_reason(self, instance, instance_id, desired, reap_requested):
        """Why this instance should go, or None to keep it."""
        if instance_id in reap_requested:
            return "replacement requested"
        if int(instance["replica_index"]) >= desired:
            return f"surplus to desired count {desired}"
        if not self._is_healthy(instance, instance_id):
            return "unhealthy"
        return None

    def reconcile_all(self):
        """Reconcile every agent in the config."""
        for agent_name in self.context.agent_specs:
            try:
                self._reap(agent_name)
            except Exception as e:
                logger.warning("Failed to reap agent %s: %s", agent_name, e)
        try:
            self._fill()
        except Exception as e:
            logger.warning("Failed to provision missing instances: %s", e)

    # ------------------------------------------------------------------ #
    #  Loop                                                              #
    # ------------------------------------------------------------------ #

    def run(self):
        logger.info(
            "Reconciler started for %d agent(s), sweeping every %ds.",
            len(self.context.agent_specs),
            self.sweep_interval,
        )
        self.reconcile_all()
        last_sweep = time.time()

        while _running:
            try:
                signals = state.drain(self.context.redis, timeout=WAKE_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning("Failed to read the wake queue: %s", e)
                time.sleep(WAKE_TIMEOUT_SECONDS)
                continue

            if state.WAKE_ALL in signals:
                self.reconcile_all()
                last_sweep = time.time()
                signals.discard(state.WAKE_ALL)

            for agent_name in sorted(signals):
                try:
                    self.reconcile(agent_name)
                except Exception as e:
                    logger.warning("Failed to reconcile agent %s: %s", agent_name, e)

            if time.time() - last_sweep >= self.sweep_interval:
                self.reconcile_all()
                last_sweep = time.time()

        logger.info("Reconciler exiting.")


def main(argv=None):
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="Ventis reconciliation loop.")
    parser.add_argument(
        "-c", "--config", required=True, help="Path to the YAML config file."
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.config):
        logger.critical("Config file not found at %s", args.config)
        return 1

    Reconciler(args.config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
