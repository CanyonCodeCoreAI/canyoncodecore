import fnmatch
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, text

import ventis.controller.utils.demo_obfuscation as demo_obfuscation
import ventis.controller.utils.sqlalchemy as sqlmod


def _parse_shifted(stored):
    """Turn a stored TIMESTAMPTZ back into the Unix epoch seconds it holds, so
    tests can assert against the shift the writer was supposed to apply."""
    return datetime.fromisoformat(str(stored)).replace(tzinfo=timezone.utc).timestamp()


class _FakeRedis:
    def __init__(self, hashes, sets=None):
        self.hashes = hashes
        self.sets = sets or {}

    def scan_keys(self, pattern):
        return [k for k in self.hashes if fnmatch.fnmatch(k, pattern)]

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def get(self, name):
        return self.hashes.get(name)

    def sadd(self, name, *values):
        self.sets.setdefault(name, set()).update(values)

    def srem(self, name, *values):
        self.sets.get(name, set()).difference_update(values)

    def smembers(self, name):
        return set(self.sets.get(name, set()))


class RuntimeSqlalchemyTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        sqlmod._engine = None
        sqlmod._project_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        sqlmod._engine = None
        sqlmod._project_id = None
        os.unlink(self.db.name)

    def test_pull_and_upsert(self):
        redis = _FakeRedis(
            {
                "future:abc": {
                    "id": "abc",
                    "request_id": "req1",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "parent": "aabbccddeeff00112233445566778899",
                    "created_at": "1.0",
                    "cpu_resource": "2.0",
                    "gpu_resource": "3.0",
                    "queue_time": "0.5",
                    "failed": "0",
                },
                "request:req1:workflow": "main",
                "future:abc:consumers": {"x": "1"},
            }
        )
        rows = sqlmod.pull_runtime_information(redis)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["future_id"], "abc")

        # Still in-flight (no finished_at yet) -- must not insert a premature
        # snapshot with a fabricated finish time and zeroed cpu/gpu.
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='abc'")
            ).fetchone()
        self.assertIsNone(row)

        rows[0]["finished_at"] = "9.0"
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='abc'")
            ).mappings().fetchone()
        self.assertEqual(row["execution_time_ms"], 8000)
        self.assertEqual(row["cpu"], 2.0)
        self.assertEqual(row["gpu"], 3.0)
        self.assertEqual(row["project_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["queue_time_ms"], 500)
        self.assertEqual(row["parent_id"], "aabbccddeeff00112233445566778899")
        self.assertEqual(bool(row["failed"]), False)
        # Stored times are shifted back by the session's deterministic offset; the
        # 8s execution window asserted above is what survives the shift.
        self.assertEqual(
            _parse_shifted(row["finished_at"]),
            9.0 - demo_obfuscation.shift_for_session("req1"),
        )
        self.assertIsNotNone(row["created_at"])

    def test_parent_id_defaults_to_none_when_absent(self):
        redis = _FakeRedis(
            {
                "future:solo": {
                    "id": "solo",
                    "request_id": "req9",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
                "request:req9:workflow": "wf9",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT parent_id FROM runtime_information WHERE future_id='solo'"
                )
            ).fetchone()
        self.assertIsNone(row[0])

    def test_observed_cpu_and_gpu_are_recorded(self):
        redis = _FakeRedis(
            {
                "future:xyz": {
                    "id": "xyz",
                    "request_id": "req2",
                    "agent": "aabbccddeeff00112233445566778899",
                    "created_at": "10.0",
                    "finished_at": "12.0",
                    "execution_time": "1.25",
                    "cpu_resource": "37.5",
                    "gpu_resource": "62.5",
                },
                "request:req2:workflow": "wf2",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT execution_time_ms, cpu, gpu "
                    "FROM runtime_information WHERE future_id='xyz'"
                )
            ).fetchone()
        self.assertEqual(row[0], 2000)
        self.assertEqual(row[1], 37.5)
        self.assertEqual(row[2], 62.5)

    def test_llm_token_and_error_fields_are_recorded(self):
        redis = _FakeRedis(
            {
                "future:llm1": {
                    "id": "llm1",
                    "request_id": "req4",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                    "input_token_count": "120",
                    "output_token_count": "45",
                    "token_count": "165",
                    "errors": "1",
                    "input_cache_tokens": "30",
                },
                "request:req4:workflow": "wf4",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT input_token_count, output_token_count, token_count, errors, "
                    "cache_hit_ratio, cached_tokens "
                    "FROM runtime_information WHERE future_id='llm1'"
                )
            ).fetchone()
        self.assertEqual(row[0], 120)
        self.assertEqual(row[1], 45)
        self.assertEqual(row[2], 165)
        self.assertEqual(row[3], 1)
        self.assertAlmostEqual(row[4], 30 / 165)
        self.assertEqual(row[5], 30)

    def test_llm_token_and_error_fields_default_when_absent(self):
        redis = _FakeRedis(
            {
                "future:nollm": {
                    "id": "nollm",
                    "request_id": "req5",
                    "agent": "00112233445566778899aabbccddeeff",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
                "request:req5:workflow": "wf5",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT input_token_count, output_token_count, token_count, errors, "
                    "cache_hit_ratio, cached_tokens "
                    "FROM runtime_information WHERE future_id='nollm'"
                )
            ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], 0.0)
        self.assertEqual(row[5], 0)

    def test_cpu_and_gpu_default_to_zero_when_not_observed(self):
        redis = _FakeRedis(
            {
                "future:noop": {
                    "id": "noop",
                    "request_id": "req3",
                    "agent": "00112233445566778899aabbccddeeff",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
                "request:req3:workflow": "wf3",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT cpu, gpu "
                    "FROM runtime_information WHERE future_id='noop'"
                )
            ).fetchone()
        self.assertEqual(row[0], 0.0)
        self.assertEqual(row[1], 0.0)

    def test_total_cost_computed_from_model_token_pricing(self):
        redis = _FakeRedis(
            {
                "future:cost1": {
                    "id": "cost1",
                    "request_id": "req6",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                    "model": "anthropic.claude-haiku-4-5-v1:0",
                    "input_token_count": "1000000",
                    "output_token_count": "1000000",
                    "token_count": "2000000",
                },
                "request:req6:workflow": "wf6",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT total_cost, token_cost FROM runtime_information "
                    "WHERE future_id='cost1'"
                )
            ).fetchone()
        # anthropic.claude-haiku-4-5-v1:0 is priced at $1/$5 per million input/output tokens.
        self.assertAlmostEqual(row[0], 6.0)
        self.assertAlmostEqual(row[1], 6.0)

    def test_total_cost_defaults_to_zero_for_unknown_model(self):
        redis = _FakeRedis(
            {
                "future:cost2": {
                    "id": "cost2",
                    "request_id": "req7",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                    "input_token_count": "1000000",
                    "output_token_count": "1000000",
                },
                "request:req7:workflow": "wf7",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT total_cost FROM runtime_information WHERE future_id='cost2'")
            ).fetchone()
        self.assertEqual(row[0], 0.0)

    def test_total_cost_includes_server_cost_from_agent_instance_type(self):
        redis = _FakeRedis(
            {
                "future:cost3": {
                    "id": "cost3",
                    "request_id": "req8",
                    "agent": "ec2agent1",
                    "created_at": "0.0",
                    "finished_at": "3600.0",
                },
                "request:req8:workflow": "wf8",
                "agent:ec2agent1:instance_type": "m5.large",
            }
        )

        rows = sqlmod.pull_runtime_information(redis)
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT total_cost, server_cost FROM runtime_information WHERE future_id='cost3'")
            ).fetchone()
        # m5.large is $0.096/hr; a full hour of execution_time should bill the full rate.
        self.assertAlmostEqual(row[0], 0.096)
        self.assertAlmostEqual(row[1], 0.096)

    def test_demo_cost_multipliers_scale_costs_independently_and_warn(self):
        redis = _FakeRedis(
            {
                "future:cost4": {
                    "id": "cost4",
                    "request_id": "req9",
                    "agent": "ec2agent2",
                    "created_at": "0.0",
                    "finished_at": "3600.0",
                    "model": "anthropic.claude-haiku-4-5-v1:0",
                    "input_token_count": "1000000",
                    "output_token_count": "1000000",
                    "token_count": "2000000",
                },
                "request:req9:workflow": "wf9",
                "agent:ec2agent2:instance_type": "m5.large",
            }
        )
        rows = sqlmod.pull_runtime_information(redis)

        os.environ["VENTIS_DEMO_TOKEN_COST_MULTIPLIER"] = "2"
        os.environ["VENTIS_DEMO_SERVER_COST_MULTIPLIER"] = "3"
        try:
            with self.assertLogs("ventis.controller.utils.sqlalchemy", level="WARNING") as cm:
                sqlmod.send_runtime_information(rows, redis)
            self.assertTrue(
                any("VENTIS_DEMO_TOKEN_COST_MULTIPLIER" in msg for msg in cm.output)
            )
            self.assertTrue(
                any("VENTIS_DEMO_SERVER_COST_MULTIPLIER" in msg for msg in cm.output)
            )
        finally:
            del os.environ["VENTIS_DEMO_TOKEN_COST_MULTIPLIER"]
            del os.environ["VENTIS_DEMO_SERVER_COST_MULTIPLIER"]

        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT total_cost, server_cost, token_cost FROM runtime_information "
                    "WHERE future_id='cost4'"
                )
            ).fetchone()
        # m5.large is $0.096/hr, scaled 3x. Haiku 4.5 is $1.00/$5.00 per million
        # tokens (1M in + 1M out = $6.00), scaled 2x.
        self.assertAlmostEqual(row[1], 0.096 * 3)
        self.assertAlmostEqual(row[2], 6.00 * 2)
        self.assertAlmostEqual(row[0], row[1] + row[2])

    def test_send_agent_information_inserts_and_updates(self):
        row = {
            "agent_id": "local:AgentA:0",
            "agent_name": "AgentA",
            "provider": "local",
            "host": "localhost",
            "host_port": "50051",
            "status": "healthy",
            "cpu_percent": "12.5",
            "gpu_percent": "0.0",
            "disk_percent": "45.0",
            "memory_percent": "60.0",
            "uptime_seconds": "100.0",
            "queue_length": "3",
            "requests_served": "5",
            "throughput": "1.0",
            "full_failures": "2",
            "error_count": "4",
            "updated_at": "1.0",
        }
        sqlmod.send_agent_information([row])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT health, cpu_percent, disk_percent, memory_percent, queue_length, "
                    "requests_served, throughput, updated_at, "
                    "full_failures, error_count, name "
                    "FROM agent_information WHERE agent_id='local:AgentA:0'"
                )
            ).fetchone()
        self.assertEqual(fetched[0], "healthy")
        self.assertEqual(fetched[1], 12.5)
        self.assertEqual(fetched[2], 45.0)
        self.assertEqual(fetched[3], 60.0)
        self.assertEqual(fetched[4], 3)
        self.assertEqual(fetched[5], 5)
        self.assertEqual(fetched[6], 1.0)
        # Agent rows get a fresh random shift per write (not the deterministic
        # per-session one), so only the window is assertable.
        self.assertGreaterEqual(
            _parse_shifted(fetched[7]),
            1.0 - demo_obfuscation.RANDOM_SHIFT_MAX_SECONDS,
        )
        self.assertLessEqual(_parse_shifted(fetched[7]), 1.0)
        self.assertEqual(fetched[8], 2)
        self.assertEqual(fetched[9], 4)
        self.assertEqual(fetched[10], "AgentA")

        row["status"] = "unhealthy"
        row["cpu_percent"] = "99.0"
        row["disk_percent"] = "90.0"
        row["memory_percent"] = "80.0"
        row["queue_length"] = "7"
        row["requests_served"] = "0"
        row["throughput"] = "0.0"
        row["full_failures"] = "0"
        row["error_count"] = "0"
        row["updated_at"] = "5.0"
        sqlmod.send_agent_information([row])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT health, cpu_percent, disk_percent, memory_percent, queue_length, "
                    "requests_served, throughput, updated_at, full_failures, error_count, name "
                    "FROM agent_information WHERE agent_id='local:AgentA:0'"
                )
            ).fetchone()
        self.assertEqual(fetched[0], "unhealthy")
        self.assertEqual(fetched[1], 99.0)
        self.assertEqual(fetched[2], 90.0)
        self.assertEqual(fetched[3], 80.0)
        self.assertEqual(fetched[4], 7)
        self.assertEqual(fetched[5], 0)  # reset to 0 after the poll interval drained it
        self.assertEqual(fetched[6], 0.0)
        self.assertGreaterEqual(
            _parse_shifted(fetched[7]),
            5.0 - demo_obfuscation.RANDOM_SHIFT_MAX_SECONDS,
        )
        self.assertLessEqual(_parse_shifted(fetched[7]), 5.0)
        self.assertEqual(fetched[8], 0)  # failures reset to 0 after the poll interval drained it
        self.assertEqual(fetched[9], 0)  # errors reset to 0 after the poll interval drained it
        self.assertEqual(fetched[10], "AgentA")

    def test_send_agent_information_defaults_missing_fields(self):
        sqlmod.send_agent_information([{"agent_id": "local:AgentB:0"}])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT health, cpu_percent, gpu_percent, disk_percent, memory_percent, "
                    "queue_length, requests_served, throughput, "
                    "full_failures, error_count, name "
                    "FROM agent_information WHERE agent_id='local:AgentB:0'"
                )
            ).fetchone()
        self.assertEqual(fetched[0], "unknown")
        self.assertEqual(fetched[1], 0.0)
        self.assertEqual(fetched[2], 0.0)
        self.assertEqual(fetched[3], 0.0)
        self.assertEqual(fetched[4], 0.0)
        self.assertEqual(fetched[5], 0)
        self.assertEqual(fetched[6], 0)
        self.assertEqual(fetched[7], 0.0)
        self.assertEqual(fetched[8], 0)
        self.assertEqual(fetched[9], 0)
        self.assertIsNone(fetched[10])

    def test_send_agent_information_noop_on_empty_rows(self):
        sqlmod.send_agent_information([])
        sqlmod.send_agent_information(None)
        with sqlmod._get_engine("").connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM agent_information")
            ).scalar()
        self.assertEqual(count, 0)

if __name__ == "__main__":
    unittest.main()
