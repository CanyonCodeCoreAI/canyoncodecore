# Sandbox Executor Agent
#
# Fourth stage. Runs surviving candidates against a SMALL sampled replica of
# the real dataset, then votes on the best via result self-consistency. This
# is the "test on a smaller database for correctness" step.
#
# Resource profile: a stateful, warm pool of sampled databases. Marked
# `stateful: true` in the controller config so all calls within one request_id
# route to the same instance (session affinity), which keeps the seeded sample
# warm across generate -> validate -> execute for that request.
#
# We use an in-memory SQLite DB seeded with a sample so the candidates are
# ACTUALLY executed and compared — correctness voting here is real, not faked.

import sqlite3


class SandboxExecutorAgent(object):
    def __init__(self):
        self.tools = [self.run_on_sample, self.select_best]
        # One warm connection per instance = the pooled, stateful resource.
        # Real impl seeds a sampled Postgres/DuckDB replica of the target DB.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._seed_sample()

    def _seed_sample(self):
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE customers (id INTEGER, name TEXT, region TEXT);
            CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount REAL, status TEXT);
            INSERT INTO customers VALUES
                (1,'Acme','US'),(2,'Globex','US'),(3,'Initech','EU'),(4,'Umbrella','EU');
            INSERT INTO orders VALUES
                (1,1,100.0,'paid'),(2,1,50.0,'paid'),(3,2,200.0,'paid'),
                (4,3,75.0,'paid'),(5,4,25.0,'refunded');
            """
        )
        self._conn.commit()

    def run_on_sample(self, sql: str) -> dict:
        """Execute a candidate against the sampled replica and return results."""
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            # Normalize rows so equivalent results compare equal regardless of
            # row ordering — the basis for self-consistency voting.
            normalized = sorted(tuple(r) for r in rows)
            return {
                "sql": sql,
                "ok": True,
                "row_count": len(rows),
                "rows": normalized,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - report any SQL error
            return {"sql": sql, "ok": False, "row_count": 0, "rows": [], "error": str(exc)}

    def select_best(self, results: list) -> dict:
        """Pick the best candidate by result self-consistency (majority vote).

        Candidates that error out are discarded. Among those that run, the
        result set produced by the most candidates wins — the assumption being
        that independently-generated queries agreeing on an answer are likelier
        correct.
        """
        runnable = [r for r in results if r.get("ok")]
        if not runnable:
            return {"selected": None, "reason": "no candidate executed successfully"}

        # Tally identical result sets.
        buckets = {}
        for r in runnable:
            key = repr(r["rows"])
            buckets.setdefault(key, []).append(r)

        winning = max(buckets.values(), key=len)
        return {
            "selected": winning[0]["sql"],
            "votes": len(winning),
            "total_runnable": len(runnable),
            "reason": "majority result agreement",
        }


if __name__ == "__main__":
    agent = SandboxExecutorAgent()
    cands = [
        "SELECT region, SUM(amount) AS total FROM customers c "
        "JOIN orders o ON o.customer_id = c.id GROUP BY region",
        "SELECT c.region, SUM(o.amount) AS total FROM orders o "
        "JOIN customers c ON c.id = o.customer_id GROUP BY c.region",
        "SELECT region FROM customers GROUP BY region",  # different answer
    ]
    res = [agent.run_on_sample(s) for s in cands]
    print(agent.select_best(res))