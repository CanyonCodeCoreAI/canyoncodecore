# Testing `porting-to-ventis` across 100 repositories

CAN-238 design. Written 2026-08-27.

## What this is for

`.claude/skills/porting-to-ventis/` claims that an arbitrary agent project can be
moved onto Ventis by writing four files beside an untouched source tree. The claim
has been checked against one project (`examples/joke_writer`). This harness checks
it against a hundred, and produces per-repo evidence of where the claim broke.

The output is not a pass rate on its own. It is a table of *how far each repo got*
and *what stopped it*, partitioned by whether the blame lies with the skill, with
Ventis, or with the repo.

## The measurement problem, and what follows from it

Two constraints shape everything below. Both come from the skill's own rules.

**The source tree may not be edited.** M19 (`NEVER edit the source tree`) and M20
(`NEVER swap the LLM provider the source uses`) are rules the skill is being
tested on. A harness that rewrites each repo's model calls onto Bedrock before
running the skill is not testing the skill — it is testing the rewrite, and every
downstream failure becomes unattributable. So the source tree is read-only for
the entire pipeline, and the LLM problem is solved outside it (§3).

**The skill may not change mid-run.** If the skill is edited between repo 1 and
repo 100, the two were not given the same test, the pass rate has no denominator,
and a fix that merely relocates a failure looks like a fix. So `tests.skill_sha`
is pinned on every row and the harness never writes to the skill. Fixes happen
between runs, as a new pinned version, with the affected repos re-run. Which
corner cases a new version closed is then a diff between two runs — which is what
CAN-237 wants anyway.

## 1. Pipeline

Eight stages. `tests.farthest_step` is the last one that passed.

| # | Stage | What runs | Fails when |
|---|-------|-----------|-----------|
| 1 | `fetched` | `git clone --depth 1`, record SHA | repo gone, too large, no license |
| 2 | `screened` | static scan: framework, LLM provider, hardcoded model ids, dependency shape | repo is out of scope for this run |
| 3 | `wired` | write `.env`, ensure model shim is up | no Bedrock credential, unmappable model |
| 4 | `ported` | **`claude -p`** running `porting-to-ventis` | agent gives up, budget exhausted, timeout |
| 5 | `validated` | `validate.py <repo>` | contract violation the agent introduced |
| 6 | `built` | `ventis build` + both probes from SKILL.md Step 4 | image builds but container cannot import |
| 7 | `deployed` | `ventis deploy` | port/config/policy failure |
| 8 | `served` | `POST /main` → `GET /status/<id>` | `"No agent loaded"`, provider error, wrong shape |

**Only stage 4 uses an agent.** Everything else is a deterministic subprocess with
a timeout. This is the property that makes failures attributable: a stage 6 failure
is a fact about the port, not about how the agent happened to behave that day.

Stage 6 runs *both* probes from SKILL.md Step 4, in order, because neither covers
the other — probe 1 (`import local_controller`) catches the protobuf/gRPC wall
before the agent is ever reached; probe 2 (`_load_agent`-shaped import) catches
the failures that otherwise surface only as `"No agent loaded"` at stage 8.

## 2. Driving Claude Code

A `claude -p` subprocess per repo, concurrency 2 until the pipeline is proven.

```
claude -p "<port instruction>" \
  --bare \
  --setting-sources "" \
  --permission-mode bypassPermissions \
  --output-format stream-json --verbose \
  --model <pinned> --effort <pinned> \
  --max-budget-usd <cap> \
  --no-session-persistence
```

Every flag above was checked against `claude --help` on the machine that will run
it, not recalled.

- **`--bare` is not optional.** It suppresses hooks, auto-memory, plugin sync and
  CLAUDE.md auto-discovery. Without it the operator's personal `~/.claude/CLAUDE.md`
  and accumulated auto-memory enter all 100 runs, vary between them, and are
  invisible in the results. Under `--bare` auth is strictly `ANTHROPIC_API_KEY`.
- **`--setting-sources ""`** keeps user/project/local settings out for the same
  reason.
- **`--max-budget-usd`** is the containment mechanism; this CLI has no `--max-turns`.
  A budget-exhausted run is recorded as its own failure mode, not as a crash.
- **The skill is delivered explicitly**, by copying
  `.claude/skills/porting-to-ventis/` into each repo working directory, so the
  version under test is the version recorded — never whatever is globally installed.
- **Tool restriction is unresolved and must be measured.** There are reports that
  under `bypassPermissions`, `--allowedTools` is ignored and only `--disallowedTools`
  constrains the tool set. This is verified on the first repo before the run scales;
  it is not assumed in either direction.

`--output-format stream-json` is written to `tests.trace_path`. The trace is the
only record of *how* the agent reached its result and is what makes a skill defect
diagnosable after the fact.

## 3. Reaching Bedrock without touching the source

Verified against AWS documentation on 2026-08-27:

| Source SDK | Base URL | Auth header |
|---|---|---|
| `openai` / `ChatOpenAI` | `https://bedrock-runtime.{region}.amazonaws.com/openai/v1` | `Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK` |
| `anthropic` / `ChatAnthropic` | `https://bedrock-runtime.{region}.amazonaws.com/anthropic` | `x-api-key: $AWS_BEARER_TOKEN_BEDROCK` |

Both surfaces support client-side tool use. Both are reachable by environment
variable alone, which is why the source tree never needs an edit.

**Model coverage is asymmetric and constrains repo selection.** Counted from
Bedrock's API-compatibility tables: 40 models serve Chat Completions (OpenAI, Qwen,
Mistral, Google, Z.AI, NVIDIA, DeepSeek, MiniMax, xAI, Moonshot); 7 serve the
Messages API, all Anthropic Claude. No Claude model serves Chat Completions, and
Meta / Amazon / Cohere / AI21 serve neither. A `ChatOpenAI` repo therefore lands on
a gpt-oss / Qwen / Mistral class model, never on Claude.

**The model id is the one thing an env var cannot reach.** A repo writes
`ChatOpenAI(model="gpt-4o-mini")`; the id travels in the request body, and Bedrock
rejects it. The fix is a shim in front of Bedrock that **rewrites the `model` field
and forwards everything else unchanged**. No protocol translation is involved —
Bedrock speaks both wire formats natively — so this is a small addition to the
`llm_proxy` skeleton on PR #54 (`core.proxy_request`, `providers/base.HttpProvider`),
not a new subsystem. Its `hooks.py` seam yields per-repo token accounting for free.

The mapping from observed model id to Bedrock model id is shim configuration,
recorded per run, so a result can always be read against the model that produced it.

Credentials reach the containers through `env_file:` (PR #53, merged into this
branch), which is the only sanctioned path — M18 forbids baking a key into the
build context.

**Prerequisite:** no AWS credential exists on the target machine today
(`~/.aws/` holds no credentials file; neither `AWS_BEARER_TOKEN_BEDROCK` nor
`AWS_ACCESS_KEY_ID` is set). Stage 3 cannot run until a Bedrock API key exists.

## 4. Storage

SQLite. The two tables from the ticket, plus the fields the two constraints above require.

```sql
CREATE TABLE repos (
  id          INTEGER PRIMARY KEY,
  repo        TEXT UNIQUE NOT NULL,   -- github url
  stars       INTEGER,
  framework   TEXT,                   -- langchain|langgraph|crewai|autogen|plain|adk
  is_multiagent INTEGER,
  description TEXT
);

CREATE TABLE tests (
  id            INTEGER PRIMARY KEY,
  repo          TEXT NOT NULL REFERENCES repos(repo),
  repo_sha      TEXT NOT NULL,        -- pins the source under test
  skill_sha     TEXT NOT NULL,        -- pins the skill under test
  farthest_step TEXT NOT NULL,        -- the stage enum of §1
  core_issue    TEXT,                 -- json: Ventis defects
  skill_issue   TEXT,                 -- json: skill defects
  analysis      TEXT,                 -- AI recap
  status        TEXT NOT NULL,        -- passed|failed|blocked|budget_exhausted|timeout
  cost_usd      REAL,
  duration_s    REAL,
  trace_path    TEXT
);
```

`core_issue` and `skill_issue` are separate columns on purpose: "Ventis cannot do
this" and "the skill fails to say this" are different findings with different
owners, and collapsing them loses the distinction the run exists to produce.

`(repo_sha, skill_sha)` is what makes two runs comparable and two *different* runs
diffable.

## 5. Scope of the first version

Stages 1–8 straight through, concurrency fixed at 2, repo list supplied by hand —
two repos from `langchain-samples`.

Deliberately excluded until the pipeline is proven: GitHub search and automated
repo selection, retry policy, parallelism above 2, and any cross-repo aggregation
beyond the raw table. These are worth writing once the failure modes are known and
not before.

## 6. Rejected alternatives

**Rewriting each repo's LLM calls onto Bedrock before the port** — the literal
reading of the ticket plan. Rejected: it violates M19 and M20, which are rules
under test, and it contaminates every downstream stage. `examples/joke_writer`'s
README already records this conclusion for the one project where the rewrite was
done deliberately: it "is not something the `porting-to-ventis` skill should do on
a user's project — it is the credential wall, and the skill's instruction is to
report it."

**A protocol-translating proxy** (OpenAI/Anthropic wire format → Bedrock Converse).
Rejected as unnecessary: Bedrock serves both wire formats natively, so only the
model id needs rewriting.

**The Claude Agent SDK as the driver.** Considered for its structured event stream.
Rejected for the first version: a `claude -p` subprocess gives the same trace via
`--output-format stream-json` with one less dependency, and the pipeline's
attribution comes from stages 5–8 being deterministic rather than from finer
introspection of stage 4.

**Restricting the run to repos already on Bedrock.** Rejected: too few exist to
reach 100, and selecting for them would bias the sample toward projects that never
exercise the credential wall the skill has the most to say about.
