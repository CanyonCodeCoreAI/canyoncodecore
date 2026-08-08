import fnmatch
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, text

import ventis.controller.utils.sqlalchemy as sqlmod


def _parse_shifted(stored):
    """Turn a stored TIMESTAMPTZ back into the Unix epoch seconds it holds."""
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

    def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)
            self.sets.pop(key, None)


class RuntimeSqlalchemyTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        sqlmod._engines = {}
        sqlmod._project_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        sqlmod._engines = {}
        sqlmod._project_id = None
        os.unlink(self.db.name)

    def test_pull_and_upsert(self):
        redis = _FakeRedis(
            {
                "future:abc:metrics": {
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
        self.assertEqual(_parse_shifted(row["finished_at"]), 9.0)
        self.assertIsNotNone(row["created_at"])

    def test_one_bad_row_does_not_block_the_rest_of_the_batch(self):
        # The middle row has a malformed finished_at (simulating any row whose
        # underlying data is broken -- e.g. its session row disappeared and
        # whatever produced this field failed) that raises when parsed. It must
        # not roll back the two good rows sharing this same send_runtime_information
        # call, and pull_runtime_information() never removes source keys, so if
        # this row silently killed the whole batch it would keep doing so forever.
        redis = _FakeRedis(
            {
                "future:good1:metrics": {
                    "id": "good1",
                    "request_id": "reqgood1",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
                "future:bad:metrics": {
                    "id": "bad",
                    "request_id": "reqbad",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "not-a-timestamp",
                },
                "future:good2:metrics": {
                    "id": "good2",
                    "request_id": "reqgood2",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
            }
        )
        rows = sqlmod.pull_runtime_information(redis)
        self.assertEqual(len(rows), 3)

        with self.assertLogs(
            "ventis.controller.utils.sqlalchemy", level="WARNING"
        ) as cm:
            sqlmod.send_runtime_information(rows, redis)
        self.assertTrue(
            any("bad" in msg for msg in cm.output),
            f"expected a warning naming the failed row, got: {cm.output}",
        )

        with sqlmod._get_engine("").connect() as conn:
            future_ids = {
                row[0]
                for row in conn.execute(
                    text("SELECT future_id FROM runtime_information")
                ).fetchall()
            }
        self.assertNotIn("bad", future_ids)
        # The good rows on either side of the bad one must still have committed --
        # a single bad row must not roll back the rest of the batch's transaction.
        self.assertIn("good1", future_ids)
        self.assertIn("good2", future_ids)

    def test_parent_id_defaults_to_none_when_absent(self):
        redis = _FakeRedis(
            {
                "future:solo:metrics": {
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
                "future:xyz:metrics": {
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
                "future:llm1:metrics": {
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
                "future:nollm:metrics": {
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
                "future:noop:metrics": {
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
                "future:cost1:metrics": {
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
                "future:cost2:metrics": {
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
                "future:cost3:metrics": {
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
                "future:cost4:metrics": {
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
        self.assertEqual(_parse_shifted(fetched[7]), 1.0)
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
        self.assertEqual(_parse_shifted(fetched[7]), 5.0)
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

    def test_send_agent_information_one_bad_row_does_not_block_the_rest(self):
        good1 = {"agent_id": "local:Good1:0", "updated_at": "1.0"}
        bad = {"agent_id": "local:Bad:0", "updated_at": "not-a-timestamp"}
        good2 = {"agent_id": "local:Good2:0", "updated_at": "1.0"}

        with self.assertLogs(
            "ventis.controller.utils.sqlalchemy", level="WARNING"
        ) as cm:
            sqlmod.send_agent_information([good1, bad, good2])
        self.assertTrue(any("Bad" in msg for msg in cm.output))

        with sqlmod._get_engine("").connect() as conn:
            agent_ids = {
                row[0]
                for row in conn.execute(
                    text("SELECT agent_id FROM agent_information")
                ).fetchall()
            }
        self.assertNotIn("local:Bad:0", agent_ids)
        self.assertIn("local:Good1:0", agent_ids)
        self.assertIn("local:Good2:0", agent_ids)

    def test_instance_type_lookup_is_cached_per_agent_across_rows(self):
        # Three rows from the same agent_id used to trigger three identical
        # redis_client.get(f"agent:{agent_id}:instance_type") calls -- one per row.
        # A single poll's rows are frequently dominated by one agent (e.g. a
        # fan-out agent that runs once per item per request), so this was the
        # majority of the redundant round trips. Assert it's fetched once.
        redis = _FakeRedis(
            {
                "future:f1:metrics": {
                    "id": "f1", "request_id": "req1",
                    "agent": "sharedagent", "created_at": "0.0", "finished_at": "1.0",
                },
                "future:f2:metrics": {
                    "id": "f2", "request_id": "req2",
                    "agent": "sharedagent", "created_at": "0.0", "finished_at": "1.0",
                },
                "future:f3:metrics": {
                    "id": "f3", "request_id": "req3",
                    "agent": "sharedagent", "created_at": "0.0", "finished_at": "1.0",
                },
                "agent:sharedagent:instance_type": "m5.large",
            }
        )
        get_calls = []
        original_get = redis.get

        def _counting_get(name):
            get_calls.append(name)
            return original_get(name)

        redis.get = _counting_get

        rows = sqlmod.pull_runtime_information(redis)
        self.assertEqual(len(rows), 3)
        sqlmod.send_runtime_information(rows, redis)

        instance_type_calls = [
            c for c in get_calls if c == "agent:sharedagent:instance_type"
        ]
        self.assertEqual(
            len(instance_type_calls),
            1,
            f"expected exactly one instance_type lookup, got: {get_calls}",
        )

        with sqlmod._get_engine("").connect() as conn:
            server_costs = {
                row[0]: row[1]
                for row in conn.execute(
                    text("SELECT future_id, server_cost FROM runtime_information")
                ).fetchall()
            }
        # All three rows still got the correct, shared instance type applied.
        # m5.large is $0.096/hr; server_cost is also scaled by the demo
        # server_cost_multiplier (100000), same as every other cost test here.
        for fid in ("f1", "f2", "f3"):
            self.assertAlmostEqual(server_costs[fid], 0.096 / 3600 * 100000)

    def test_send_agent_information_noop_on_empty_rows(self):
        sqlmod.send_agent_information([])
        sqlmod.send_agent_information(None)
        with sqlmod._get_engine("").connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM agent_information")
            ).scalar()
        self.assertEqual(count, 0)


class GetEngineCachingTests(unittest.TestCase):
    """Bug B: _get_engine() must rebuild when the resolved URL changes, not cache forever.

    Was a single `_engine = None` cached forever after the first call, silently ignoring every
    later `database_url` argument -- so once reload_config() moved this process to a different
    project's database, every write kept going to the *original* one.
    """

    def setUp(self):
        self.db_a = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_a.close()
        self.db_b = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_b.close()
        os.environ.pop("VENTIS_DATABASE_URL", None)
        sqlmod._engines = {}

    def tearDown(self):
        sqlmod._engines = {}
        os.unlink(self.db_a.name)
        os.unlink(self.db_b.name)

    def test_different_urls_produce_different_engines(self):
        e1 = sqlmod._get_engine(f"sqlite:///{self.db_a.name}")
        e2 = sqlmod._get_engine(f"sqlite:///{self.db_b.name}")
        self.assertIsNot(e1, e2)

    def test_a_recurring_url_reuses_its_engine(self):
        e1 = sqlmod._get_engine(f"sqlite:///{self.db_a.name}")
        sqlmod._get_engine(f"sqlite:///{self.db_b.name}")
        e3 = sqlmod._get_engine(f"sqlite:///{self.db_a.name}")
        self.assertIs(e1, e3)


class SendRuntimeInformationClearsMetricsTests(unittest.TestCase):
    """Bug F: send_runtime_information must delete a future's own future:{fid}:metrics key
    itself, immediately after a *confirmed successful* write -- not leave that to
    _cleanup_request() on its own, unrelated cleanup_interval timer. That old split let cleanup
    delete the key before the next poll tick ever read it, permanently losing the row with no
    error anywhere. Confirmed live: a request completing in ~4s had all 4 of its futures
    cleaned up before the next 5s poll tick landed.
    """

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        os.environ["VENTIS_DATABASE_URL"] = f"sqlite:///{self.db.name}"
        sqlmod._engines = {}
        sqlmod._project_id = "11111111-1111-1111-1111-111111111111"

    def tearDown(self):
        sqlmod._engines = {}
        sqlmod._project_id = None
        os.unlink(self.db.name)

    def _row(self, future_id):
        return {
            f"future:{future_id}:metrics": {
                "id": future_id,
                "request_id": "req1",
                "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                "created_at": "1.0",
                "finished_at": "2.0",
            },
        }

    def test_a_successful_write_deletes_its_own_metrics_key(self):
        redis = _FakeRedis(self._row("solo"))
        rows = sqlmod.pull_runtime_information(redis)

        sqlmod.send_runtime_information(rows, redis)

        self.assertNotIn("future:solo:metrics", redis.hashes)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='solo'")
            ).fetchone()
        self.assertIsNotNone(row)

    def test_a_failed_write_leaves_its_metrics_key_so_it_can_retry(self):
        redis = _FakeRedis(
            {
                "future:bad:metrics": {
                    "id": "bad",
                    "request_id": "req1",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "not-a-timestamp",
                },
            }
        )
        rows = sqlmod.pull_runtime_information(redis)

        sqlmod.send_runtime_information(rows, redis)

        self.assertIn("future:bad:metrics", redis.hashes)

    def test_one_bad_row_does_not_stop_the_good_rows_from_being_cleared(self):
        redis = _FakeRedis(
            {
                "future:good:metrics": {
                    "id": "good",
                    "request_id": "reqgood",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "2.0",
                },
                "future:bad:metrics": {
                    "id": "bad",
                    "request_id": "reqbad",
                    "agent": "1f2e3d4c5b6a7988fedcba9876543210",
                    "created_at": "1.0",
                    "finished_at": "not-a-timestamp",
                },
            }
        )
        rows = sqlmod.pull_runtime_information(redis)

        sqlmod.send_runtime_information(rows, redis)

        self.assertNotIn("future:good:metrics", redis.hashes)
        self.assertIn("future:bad:metrics", redis.hashes)

    def test_a_redis_delete_failure_does_not_undo_or_block_the_db_write(self):
        class _DeleteFailsRedis(_FakeRedis):
            def delete(self, *keys):
                raise ConnectionError("redis hiccup")

        redis = _DeleteFailsRedis(self._row("solo"))
        rows = sqlmod.pull_runtime_information(redis)

        sqlmod.send_runtime_information(rows, redis)  # must not raise

        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='solo'")
            ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
