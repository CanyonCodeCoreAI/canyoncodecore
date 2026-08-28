# Testing and continually improve `porting-to-ventis` , its harness and core

## 1. Pipeline


| #   | Stage       | What runs                                                                   | Fails when                                       |
| --- | ----------- | --------------------------------------------------------------------------- | ------------------------------------------------ |
| 1   | `fetched`   | `git clone --depth 1`, record SHA                                           | repo gone, too large, no license                 |
| 2   | `screened`  | static scan: framework, LLM provider, hardcoded model ids, dependency shape | repo is out of scope for this run                |
| 3   | `wired`     | write `.env`, ensure model shim is up                                       | no Bedrock credential, unmappable model          |
| 4   | `ported`    | `**claude -p**` running `porting-to-ventis`                                 | agent gives up, budget exhausted, timeout        |
| 5   | `validated` | `validate.py <repo>`                                                        | contract violation the agent introduced          |
| 6   | `built`     | `ventis build` + both probes from SKILL.md Step 4                           | image builds but container cannot import         |
| 7   | `deployed`  | `ventis deploy`                                                             | port/config/policy failure                       |
| 8   | `served`    | `POST /main` → `GET /status/<id>`                                           | `"No agent loaded"`, provider error, wrong shape |


**Only stage 4 uses an agent.** Everything else is a deterministic subprocess with
a timeout. This is the property that makes failures attributable: a stage 6 failure
is a fact about the port, not about how the agent happened to behave that day.

Stage 6 runs *both* probes from SKILL.md Step 4, in order, because neither covers
the other — probe 1 (`import local_controller`) catches the protobuf/gRPC wall
before the agent is ever reached; probe 2 (`_load_agent`-shaped import) catches
the failures that otherwise surface only as `"No agent loaded"` at stage 8.

**Stage 5 does not gate stages 6–8.** A failed `validate.py` is recorded and the
pipeline continues. This is the only way to observe a validation that was wrong to
block — validate says no, the port would have served anyway — and that observation
cannot be recovered later, because the build never ran. Together with the opposite
case (validate passes, a later stage fails, which is visible by default) it gives
`validate.py` a confusion matrix, which is the only quantitative basis on which the
script can be improved. The cost is a few wasted builds.

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

- `**--bare` is not optional.** It suppresses hooks, auto-memory, plugin sync and
CLAUDE.md auto-discovery. Without it the operator's personal `~/.claude/CLAUDE.md`
and accumulated auto-memory enter all 100 runs, vary between them, and are
invisible in the results. Under `--bare` auth is strictly `ANTHROPIC_API_KEY`.
- `**--setting-sources ""**` keeps user/project/local settings out for the same
reason.
- `**--max-budget-usd**` is the containment mechanism; this CLI has no `--max-turns`.
A budget-exhausted run is recorded as its own failure mode, not as a crash.
- **The skill is delivered explicitly**, by copying
`.claude/skills/porting-to-ventis/` into each repo working directory, so the
version under test is the version recorded — never whatever is globally installed.
- **Tool restriction is unresolved and must be measured.** There are reports that
under `bypassPermissions`, `--allowedTools` is ignored and only `--disallowedTools`
constrains the tool set. This is verified on the first repo before the run scales;
it is not assumed in either direction.

`--output-format stream-json` is written into the run's `artifacts/` directory. The
trace is the only record of *how* the agent reached its result, and it is what makes
a skill defect diagnosable after the fact.

## 3. Reaching Bedrock without touching the source

Verified against AWS documentation on 2026-08-27:


| Source SDK                    | Base URL                                                   | Auth header                                       |
| ----------------------------- | ---------------------------------------------------------- | ------------------------------------------------- |
| `openai` / `ChatOpenAI`       | `https://bedrock-runtime.{region}.amazonaws.com/openai/v1` | `Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK` |
| `anthropic` / `ChatAnthropic` | `https://bedrock-runtime.{region}.amazonaws.com/anthropic` | `x-api-key: $AWS_BEARER_TOKEN_BEDROCK`            |


Both surfaces support client-side tool use. Both are reachable by environment
variable alone, which is why the source tree never needs an edit.

**Model coverage is asymmetric and constrains repo selection.** Counted from
Bedrock's API-compatibility tables: 40 models serve Chat Completions (OpenAI, Qwen,
Mistral, Google, Z.AI, NVIDIA, DeepSeek, MiniMax, xAI, Moonshot); 7 serve the
Messages API, all Anthropic Claude. No Claude model serves Chat Completions, and
Meta / Amazon / Cohere / AI21 serve neither. A `ChatOpenAI` repo therefore lands on
a gpt-oss / Qwen / Mistral class model, never on Claude.

**What the credential can reach is narrower still, and is an account property
rather than a property of Bedrock.** Measured on 2026-08-28 against the key in
use:


| Surface                 | Result                                                                                                   |
| ----------------------- | -------------------------------------------------------------------------------------------------------- |
| OpenAI Chat Completions | works — `openai.gpt-oss-120b-1:0`, and gpt-oss-20b, qwen3-32b, mistral-large-3, deepseek-v3.2 all answer |
| Anthropic Messages      | closed — every Messages-capable Claude answers `permission_error`                                        |


The control plane lists 121 models, which is what the platform offers and not
what the account may call: a model can appear there and still be refused. Claude 3
Haiku gives the reason — *"Model use case details have not been submitted for this
account"* — so this is an entitlement, reopened by submitting the Anthropic use
case form rather than by any change here.

Claude is reachable on this account through **Converse**, which was confirmed. It
is not a way around the closed surface: Converse is a third wire format, so
routing an Anthropic SDK call to it means the protocol translation this design
exists to avoid.

The consequence is a scope limit that must be stated with any result from this
run: **repos using the Anthropic SDK are rejected at stage 2, not tested.** The
harness expresses this as data rather than in code — a surface whose entry in
`repos.yaml` is empty is a surface the screen refuses to route to — so the day
the entitlement lands, one line of configuration brings those repos back.

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

**Credential shape.** The key in use is a short-term Bedrock bearer token: an
`ASIA...` STS credential scoped to one region, valid 12 hours. That is ample for
proving the pipeline on two repos and too short for a hundred, so a run at full
size needs either a long-term key or a refresh step. The harness reads the token
from `AWS_BEARER_TOKEN_BEDROCK` on each `wire`, so a refreshed token is picked up
by repos that have not started yet, but not by containers already running.

## 4. Storage

SQLite, holding the two tables from the ticket. The database stores **artifacts and
versions, not analysis.**

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
  repo_sha      TEXT NOT NULL,        -- which source
  skill_sha     TEXT NOT NULL,        -- which skill
  ventis_sha    TEXT NOT NULL,        -- which core
  farthest_step TEXT NOT NULL,        -- the stage enum of §1
  status        TEXT NOT NULL,        -- passed|failed|blocked|budget_exhausted|timeout
  validate_ok   INTEGER,              -- stage 5's verdict, kept apart from the outcome
  core_issue    TEXT,                 -- json: Ventis defects
  skill_issue   TEXT,                 -- json: skill defects
  analysis      TEXT,                 -- AI recap
  cost_usd      REAL,
  artifacts     TEXT NOT NULL         -- directory: trace, the four written files,
                                      -- validate output, per-stage stderr
);
```

The three SHAs are the only things that cannot be reconstructed afterwards — once
the run is over, which skill and which core produced a result is unrecoverable.
Everything else about *why* a repo failed is computed later by reading `artifacts/`.

`validate_ok` is a separate column from `farthest_step` so the two can be joined:
that join is the confusion matrix of §1.

`core_issue` and `skill_issue` stay separate columns: "Ventis cannot do this" and
"the skill fails to say this" are findings with different owners, and collapsing
them loses the distinction the run exists to produce.

Deliberately **not** in the schema: per-check validation statistics, agent-behaviour
counts (which skill files were read, how many edits were retried), and the
deterministic audits of the MUST rules a machine can decide (M19's clean
`git status` on the source, M20's unchanged provider imports). All of these are
derivable from `artifacts/` by a script, at any time, without re-running anything —
and running the pipeline is the expensive part. Write those scripts when there is a
corpus worth aggregating and it is clear what to aggregate.

## 4a. What the corpus turned out to cost

The first screening run answered a question this design had filed as a
by-product. Of six LangChain sample repositories, **none were in scope**: five
are `src/` layouts with `pyproject.toml`, and `ventis build` could not make such
a tree importable, so no port of them could load. `src/` plus packaging metadata
is not a quirk of those five — it is the shape LangChain's own templates ship.

That made the corpus, not the harness, the binding constraint on CAN-238: a
hundred-repo run against a Ventis without an editable install would have produced
close to a hundred stage 2 rejections and tested almost nothing.

`_install_step` — a Dockerfile step that runs `pip install -e .` when the project
declares packaging metadata, handing the import root to the project rather than
making Ventis guess a directory — was ported onto this branch from
`jiajunh/can-228-create-a-skill-to-convert-a-langchain-project-to-ventis`. Four of
the six came into scope immediately.

**Ported rather than merged, deliberately.** That branch is an older parallel
line: it carries its own copy of the skill from before `validate.py` existed, its
own earlier `env_file.py`, and its own `joke_writer`. Merging it whole conflicted
on twelve files, three of them the skill — it would have regressed the artifact
under test in the act of enabling the test.

The M24 rejection is still real for repos that declare no packaging metadata at
all, and `langchain-academy` remains rejected for exactly that reason.

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

**Merging `can-228` whole to obtain the editable install.** Rejected for the
reason in section 4a: the branch carries an older copy of the artifact under
test, so the merge would have changed what the run measures. The one capability
was ported instead, and its provenance recorded in the commit.