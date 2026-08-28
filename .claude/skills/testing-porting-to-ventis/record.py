#!/usr/bin/env python3
"""Write one test result into the results database.

Reads a JSON object on stdin so that findings and analysis -- which contain
quotes, newlines and error text -- reach SQLite as data. Hand-quoting them into
a `sqlite3` heredoc is how a run's own error message ends up truncating the row
that was supposed to record it.

    python record.py --db .ventis-tests/results.sqlite <<'JSON'
    {"repo": "...", "repo_sha": "...", ..., "core_issue": [...]}
    JSON
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_FIELDS = ("stars", "framework", "is_multiagent", "description")
TEST_FIELDS = ("repo", "repo_sha", "skill_sha", "ventis_sha", "farthest_step",
               "status", "validate_ok", "core_issue", "skill_issue", "analysis",
               "artifacts", "started_at", "ended_at")
REQUIRED = ("repo", "repo_sha", "skill_sha", "ventis_sha", "farthest_step",
            "status", "artifacts", "started_at")
STATUSES = {"passed", "failed", "blocked"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--schema", default=str(Path(__file__).with_name("schema.sql")))
    args = ap.parse_args()

    row = json.load(sys.stdin)

    missing = [f for f in REQUIRED if not row.get(f)]
    if missing:
        print(f"missing required field(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    if row["status"] not in STATUSES:
        print(f"status must be one of {sorted(STATUSES)}", file=sys.stderr)
        return 2

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(Path(args.schema).read_text(encoding="utf-8"))

    with conn:
        conn.execute("INSERT OR IGNORE INTO repos (repo) VALUES (?)", (row["repo"],))
        cols = {f: row[f] for f in REPO_FIELDS if row.get(f) is not None}
        if cols:
            assigns = ", ".join(f"{k} = ?" for k in cols)
            conn.execute(f"UPDATE repos SET {assigns} WHERE repo = ?",
                         (*cols.values(), row["repo"]))

        test = {}
        for f in TEST_FIELDS:
            v = row.get(f)
            test[f] = json.dumps(v) if isinstance(v, (list, dict)) else v
        names = ", ".join(test)
        marks = ", ".join("?" for _ in test)
        cur = conn.execute(f"INSERT INTO tests ({names}) VALUES ({marks})",
                           tuple(test.values()))

    print(f"recorded test #{cur.lastrowid}: {row['repo']} -> "
          f"{row['status']} at {row['farthest_step']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
