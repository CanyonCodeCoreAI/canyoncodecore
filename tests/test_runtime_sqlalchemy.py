import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, text

import ventis.controller.utils.sqlalchemy as sqlmod


class _FakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes

    def scan_keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.hashes if k.startswith(prefix)]

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def get(self, name):
        return self.hashes.get(name)


_CREATE = """
CREATE TABLE runtime_information (
    future_id TEXT PRIMARY KEY,
    session_id TEXT,
    workflow TEXT,
    agent TEXT,
    execution_time REAL,
    cpu_resource REAL,
    gpu_resource REAL,
    created_at REAL,
    updated_at REAL,
    queue_time REAL,
    fail INTEGER,
    parent_id TEXT,
    input_token_count INTEGER,
    output_token_count INTEGER,
    token_count INTEGER,
    errors INTEGER,
    cache_hit_ratio REAL,
    total_cost REAL,
    model TEXT
)
"""


class RuntimeSqlalchemyTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        sqlmod._engine = None
        with create_engine(os.environ["VENTIS_DATABASE_URL"]).begin() as conn:
            conn.execute(text(_CREATE))

    def tearDown(self):
        sqlmod._engine = None
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

        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT execution_time, cpu_resource, gpu_resource, workflow, queue_time, "
                    "parent_id "
                    "FROM runtime_information WHERE future_id='abc'"
                )
            ).fetchone()
        self.assertGreaterEqual(row[0], 0)
        self.assertEqual(row[1], 2.0)
        self.assertEqual(row[2], 3.0)
        self.assertEqual(row[3], "main")
        self.assertEqual(row[4], 0.5)
        self.assertEqual(row[5], "aabbccddeeff00112233445566778899")

        rows[0]["finished_at"] = "9.0"
        sqlmod.send_runtime_information(rows, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='abc'")
            ).mappings().fetchone()
        self.assertEqual(row["execution_time"], 8.0)
        self.assertEqual(row["updated_at"], 9.0)
        self.assertEqual(row["queue_time"], 0.5)
        for value in row.values():
            self.assertNotIn(value, (None, ""))

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
                    "SELECT execution_time, cpu_resource, gpu_resource "
                    "FROM runtime_information WHERE future_id='xyz'"
                )
            ).fetchone()
        self.assertEqual(row[0], 2.0)
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
                    "cache_hit_ratio "
                    "FROM runtime_information WHERE future_id='llm1'"
                )
            ).fetchone()
        self.assertEqual(row[0], 120)
        self.assertEqual(row[1], 45)
        self.assertEqual(row[2], 165)
        self.assertEqual(row[3], 1)
        self.assertAlmostEqual(row[4], 30 / 165)

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
                    "cache_hit_ratio "
                    "FROM runtime_information WHERE future_id='nollm'"
                )
            ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], 0.0)

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
                    "SELECT cpu_resource, gpu_resource "
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
                    "SELECT total_cost, model FROM runtime_information "
                    "WHERE future_id='cost1'"
                )
            ).fetchone()
        # anthropic.claude-haiku-4-5-v1:0 is priced at $1/$5 per million input/output tokens.
        self.assertAlmostEqual(row[0], 6.0)
        self.assertEqual(row[1], "anthropic.claude-haiku-4-5-v1:0")

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
                text("SELECT total_cost FROM runtime_information WHERE future_id='cost3'")
            ).fetchone()
        # m5.large is $0.096/hr; a full hour of execution_time should bill the full rate.
        self.assertAlmostEqual(row[0], 0.096)

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
            "failures": "2",
            "errors": "4",
            "created_at": "1.0",
            "updated_at": "1.0",
        }
        sqlmod.send_agent_information([row])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT status, cpu_percent, disk_percent, memory_percent, queue_length, "
                    "requests_served, throughput, created_at, updated_at, "
                    "failures, errors "
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
        self.assertEqual(fetched[7], "1.0")
        self.assertEqual(fetched[8], "1.0")
        self.assertEqual(fetched[9], 2)
        self.assertEqual(fetched[10], 4)

        row["status"] = "unhealthy"
        row["cpu_percent"] = "99.0"
        row["disk_percent"] = "90.0"
        row["memory_percent"] = "80.0"
        row["queue_length"] = "7"
        row["requests_served"] = "0"
        row["throughput"] = "0.0"
        row["failures"] = "0"
        row["errors"] = "0"
        row["updated_at"] = "5.0"
        sqlmod.send_agent_information([row])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT status, cpu_percent, disk_percent, memory_percent, queue_length, "
                    "requests_served, throughput, created_at, updated_at, failures, errors "
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
        self.assertEqual(fetched[7], "1.0")  # created_at stays fixed
        self.assertEqual(fetched[8], "5.0")
        self.assertEqual(fetched[9], 0)  # failures reset to 0 after the poll interval drained it
        self.assertEqual(fetched[10], 0)  # errors reset to 0 after the poll interval drained it

    def test_send_agent_information_defaults_missing_fields(self):
        sqlmod.send_agent_information([{"agent_id": "local:AgentB:0"}])
        with sqlmod._get_engine("").connect() as conn:
            fetched = conn.execute(
                text(
                    "SELECT status, cpu_percent, gpu_percent, disk_percent, memory_percent, "
                    "uptime_seconds, queue_length, requests_served, throughput, "
                    "failures, errors "
                    "FROM agent_information WHERE agent_id='local:AgentB:0'"
                )
            ).fetchone()
        self.assertEqual(fetched[0], "unknown")
        self.assertEqual(fetched[1], 0.0)
        self.assertEqual(fetched[2], 0.0)
        self.assertEqual(fetched[3], 0.0)
        self.assertEqual(fetched[4], 0.0)
        self.assertEqual(fetched[5], 0.0)
        self.assertEqual(fetched[6], 0)
        self.assertEqual(fetched[7], 0)
        self.assertEqual(fetched[8], 0.0)
        self.assertEqual(fetched[9], 0)
        self.assertEqual(fetched[10], 0)

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
