"""SQLite storage for harness runs.

The database holds artifacts and versions, not analysis. The three SHAs are the
only things that cannot be reconstructed once a run is over; everything about
*why* a repo failed is recomputed later by reading its artifacts directory.
See DESIGN.md section 4.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# The gating stages, in order. Stage 5 (`validated`) is deliberately absent: it
# does not halt the pipeline, so it cannot be the "furthest step reached" — its
# verdict lives in tests.validate_ok instead. See DESIGN.md section 1.
STAGES = ["fetched", "screened", "wired", "ported", "built", "deployed", "served"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id            INTEGER PRIMARY KEY,
  repo          TEXT UNIQUE NOT NULL,
  stars         INTEGER,
  framework     TEXT,
  is_multiagent INTEGER,
  description   TEXT
);

CREATE TABLE IF NOT EXISTS tests (
  id            INTEGER PRIMARY KEY,
  repo          TEXT NOT NULL REFERENCES repos(repo),
  repo_sha      TEXT NOT NULL,
  skill_sha     TEXT NOT NULL,
  ventis_sha    TEXT NOT NULL,
  farthest_step TEXT NOT NULL,
  status        TEXT NOT NULL,
  validate_ok   INTEGER,
  core_issue    TEXT,
  skill_issue   TEXT,
  analysis      TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  llm_calls     INTEGER,
  cost_usd      REAL,
  artifacts     TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT
);
"""


# Columns added after the first databases were written. Cheap to add in place,
# and cheaper than re-running a repo to change a schema.
_ADDED_COLUMNS = {
    "tests": {"tokens_in": "INTEGER", "tokens_out": "INTEGER",
              "llm_calls": "INTEGER", "cost_usd": "REAL"},
}


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


def upsert_repo(conn: sqlite3.Connection, repo: str, **fields) -> None:
    """Insert the repo if new, then update whichever columns were supplied.

    Stage 2 learns most of these, so a repo row is written twice: once empty at
    fetch time, once populated after the screen.
    """
    with conn:
        conn.execute("INSERT OR IGNORE INTO repos (repo) VALUES (?)", (repo,))
        known = {"stars", "framework", "is_multiagent", "description"}
        cols = {k: v for k, v in fields.items() if k in known and v is not None}
        if cols:
            assigns = ", ".join(f"{k} = ?" for k in cols)
            conn.execute(
                f"UPDATE repos SET {assigns} WHERE repo = ?",
                (*cols.values(), repo),
            )


def record_test(conn: sqlite3.Connection, **fields) -> int:
    for key in ("core_issue", "skill_issue"):
        if isinstance(fields.get(key), (list, dict)):
            fields[key] = json.dumps(fields[key])
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with conn:
        cur = conn.execute(
            f"INSERT INTO tests ({cols}) VALUES ({marks})", tuple(fields.values())
        )
    return cur.lastrowid


def summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT repo, farthest_step, status, validate_ok,
               tokens_in, tokens_out, llm_calls, artifacts
        FROM tests ORDER BY id
        """
    ).fetchall()


def confusion(conn: sqlite3.Connection) -> dict[str, int]:
    """validate.py's confusion matrix — the point of not letting stage 5 gate.

    A false negative is a check validate.py is missing. A false positive is a
    check that was wrong to block, and is only observable because the build ran
    anyway.

    `blocked` rows are excluded. A repo stopped by its own missing backing
    service never put the port to the test, so counting it as a validation miss
    would blame validate.py for a vector store nobody configured.
    """
    rows = conn.execute(
        "SELECT validate_ok, farthest_step FROM tests "
        "WHERE validate_ok IS NOT NULL AND status != 'blocked'"
    ).fetchall()
    served = lambda r: r["farthest_step"] == "served"  # noqa: E731
    return {
        "true_positive": sum(1 for r in rows if not r["validate_ok"] and not served(r)),
        "false_positive": sum(1 for r in rows if not r["validate_ok"] and served(r)),
        "false_negative": sum(1 for r in rows if r["validate_ok"] and not served(r)),
        "true_negative": sum(1 for r in rows if r["validate_ok"] and served(r)),
    }
