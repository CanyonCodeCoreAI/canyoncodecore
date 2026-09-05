"""CAN-283: with no `database` in the config, every metrics poll used to log

    Failed to write agent information for instance <X> (localhost:800N) (non-fatal):
    Could not parse SQLAlchemy URL from given URL string

once per instance, every poll_interval (5s by default), drowning out the errors an
operator actually needs to read. Telemetry has nowhere to go without a database, so
the writes -- and the metrics that feed them -- are skipped outright.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ventis.controller.global_controller import GlobalController
from ventis.controller.utils.telemetry_logging import resolve_database_url


class EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("VENTIS_DATABASE_URL", None)

    def tearDown(self):
        os.environ.pop("VENTIS_DATABASE_URL", None)
        if self._saved is not None:
            os.environ["VENTIS_DATABASE_URL"] = self._saved


class ResolveDatabaseUrlTests(EnvIsolatedTestCase):
    def test_nothing_configured_resolves_to_none(self):
        # config.get("database", {}).get("url") yields None when the key is absent --
        # str(None) == "None" is what SQLAlchemy used to choke on.
        for value in (None, "", "   "):
            self.assertIsNone(resolve_database_url(value), repr(value))

    def test_env_var_wins_over_the_config_value(self):
        os.environ["VENTIS_DATABASE_URL"] = "sqlite:///env.db"
        self.assertEqual(
            resolve_database_url("postgresql://cfg-host/db"), "sqlite:///env.db"
        )

    def test_empty_env_var_falls_back_to_the_config_value(self):
        os.environ["VENTIS_DATABASE_URL"] = ""
        self.assertEqual(resolve_database_url("sqlite:///cfg.db"), "sqlite:///cfg.db")


class PollTelemetryTests(EnvIsolatedTestCase):
    """_poll_controllers must not touch telemetry when there is no database URL."""

    def _controller(self, config):
        controller = GlobalController.__new__(GlobalController)
        controller.config = config
        controller.poll_interval = 5
        controller._last_status = {}
        controller._last_metrics_poll_time = {}
        controller._on_controller_healthy = MagicMock()
        controller.instance_manager = MagicMock()
        controller.instance_manager.list_instances.return_value = [
            {
                "agent_name": "AgentA",
                "agent_id": "local:AgentA:0",
                "host": "localhost",
                "host_port": 8000,
            }
        ]
        self.node_redis = MagicMock()
        self.node_redis.hgetall.return_value = {"requests_served": "3"}
        self.node_redis.get.return_value = "healthy"
        controller._get_node_redis_for = MagicMock(return_value=self.node_redis)
        return controller

    def _poll(self, controller):
        with patch(
            "ventis.controller.global_controller.send_runtime_information"
        ) as send_runtime, patch(
            "ventis.controller.global_controller.send_agent_information"
        ) as send_agent, patch(
            "ventis.controller.global_controller.pull_runtime_information"
        ) as pull_runtime, self.assertLogs(
            "ventis.controller.global_controller", level="INFO"
        ) as logs:
            controller._poll_controllers()
        return send_runtime, send_agent, pull_runtime, logs.output

    def _assert_telemetry_skipped(self, config):
        controller = self._controller(config)

        send_runtime, send_agent, pull_runtime, output = self._poll(controller)

        send_runtime.assert_not_called()
        send_agent.assert_not_called()
        pull_runtime.assert_not_called()
        self.assertEqual([line for line in output if "WARNING" in line], [])
        # Nothing was persisted, so the counters stay where they are.
        self.node_redis.hset_multiple.assert_not_called()

    def test_a_missing_database_key_skips_telemetry(self):
        self._assert_telemetry_skipped({"agents": []})

    def test_an_empty_database_block_is_treated_as_no_database(self):
        # `database:` with nothing under it parses as None, not {}.
        self._assert_telemetry_skipped({"agents": [], "database": None})

    def test_a_configured_database_still_gets_both_writes(self):
        controller = self._controller(
            {"agents": [], "database": {"url": "postgresql://user@host/db"}}
        )

        send_runtime, send_agent, _, output = self._poll(controller)

        self.assertEqual(send_runtime.call_args.args[2], "postgresql://user@host/db")
        self.assertEqual(send_agent.call_args.args[1], "postgresql://user@host/db")
        self.assertEqual([line for line in output if "WARNING" in line], [])
        # Counters are cleared only once the row has actually been persisted.
        self.node_redis.hset_multiple.assert_called_once()

    def test_the_env_var_alone_is_enough_to_enable_writes(self):
        os.environ["VENTIS_DATABASE_URL"] = "sqlite:///env.db"
        controller = self._controller({"agents": []})

        send_runtime, send_agent, _, _ = self._poll(controller)

        send_runtime.assert_called_once()
        send_agent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
