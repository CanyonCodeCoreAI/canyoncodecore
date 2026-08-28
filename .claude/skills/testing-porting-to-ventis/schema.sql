-- Results of running `porting-to-ventis` against a repository.
--
-- Everything a machine can produce is a column; everything that needs judgement
-- is written by the agent that ran the port. No analysis is stored that could be
-- recomputed later from the artifacts directory -- running the pipeline is the
-- expensive part, and reading its output afterwards is not.

CREATE TABLE IF NOT EXISTS repos (
  id            INTEGER PRIMARY KEY,
  repo          TEXT UNIQUE NOT NULL,  -- github url
  stars         INTEGER,               -- gh api
  framework     TEXT,                  -- langchain|langgraph|crewai|autogen|adk|plain
  is_multiagent INTEGER,               -- does one request fan out to independent work?
  description   TEXT                   -- technical, written by the agent
);

CREATE TABLE IF NOT EXISTS tests (
  id            INTEGER PRIMARY KEY,
  repo          TEXT NOT NULL REFERENCES repos(repo),

  -- The three pins. Nothing else here can be reconstructed once a run is over:
  -- which source, which skill, and which Ventis produced this result.
  repo_sha      TEXT NOT NULL,
  skill_sha     TEXT NOT NULL,
  ventis_sha    TEXT NOT NULL,

  farthest_step TEXT NOT NULL,         -- the furthest stage reached
  status        TEXT NOT NULL,         -- passed|failed|blocked
  validate_ok   INTEGER,               -- stage 5's verdict, kept apart from the outcome

  core_issue    TEXT,                  -- json: findings a Ventis owner must fix
  skill_issue   TEXT,                  -- json: findings the skill file must fix
  analysis      TEXT,                  -- the agent's recap: what happened and why

  artifacts     TEXT NOT NULL,         -- directory holding every command's output
  started_at    TEXT NOT NULL,
  ended_at      TEXT
);
