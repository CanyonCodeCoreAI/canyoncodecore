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
    created_at TEXT,
    updated_at TEXT
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
                    "agent": "AgentA",
                    "created_at": "1.0",
                },
                "request:req1:workflow": "main",
                "future:abc:consumers": {"x": "1"},
            }
        )
        rows = sqlmod.pull_data(redis)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["future_id"], "abc")

        sqlmod.send_data(rows, {"AgentA": {"cpu": 2, "gpu": 1}}, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text(
                    "SELECT execution_time, cpu_resource, gpu_resource, workflow "
                    "FROM runtime_information WHERE future_id='abc'"
                )
            ).fetchone()
        self.assertGreaterEqual(row[0], 0)
        self.assertEqual(row[1], 2.0)
        self.assertEqual(row[2], 1.0)
        self.assertEqual(row[3], "main")

        rows[0]["finished_at"] = "9.0"
        sqlmod.send_data(rows, {"AgentA": {"cpu": 2, "gpu": 1}}, redis)
        with sqlmod._get_engine("").connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runtime_information WHERE future_id='abc'")
            ).fetchone()
        self.assertEqual(row[4], 8.0)
        self.assertEqual(row[8], "9.0")
        for value in row:
            self.assertNotIn(value, (None, ""))


if __name__ == "__main__":
    unittest.main()
