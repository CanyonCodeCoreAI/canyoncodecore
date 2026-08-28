---
name: testing-porting-to-ventis
description: Use when running a repository through the porting-to-ventis skill to find out where the port stops, or when building a corpus of such results across many repositories
---

# Testing `porting-to-ventis` against a repository

One repository per run. The output is a row in `.ventis-tests/results.sqlite`
saying how far the port got and what stopped it, plus a directory of every
command's raw output.

**A pass rate is not the deliverable.** The deliverable is attribution: for each
repository, whether the blame lies with the skill, with Ventis, or with the
repository itself. A run that ends `blocked` because the repo needs a vector
store nobody configured is not evidence about the skill, and recording it as a
failure makes the whole corpus mean less than it appears to.

## Two rules the run is built on

**Never edit the source tree.** M19 and M20 are rules the skill is being tested
on. Rewriting a repo's model calls, or setting its model config to a different
provider, tests the rewrite instead — and every later failure becomes
unattributable. Add files beside the source; leave `git status` on the source
clean.

**Never edit `porting-to-ventis` during a run.** Its git tree hash is pinned into
every row. A skill edited between repo 1 and repo 100 means the two were not
given the same test. Fix it between runs, as a new pinned version, and re-run
what it affects.

## What you judge, and what you only record

You judge two things: **whether the repo is in scope** (step 2) and **what the
result means** (the write-up). Everything else is a command whose exit code and
output you record verbatim.

**Never decide that a build "basically worked".** `ventis build` prints
`Build complete.` and exits 0 for a project whose container dies on startup, so
its exit code is not evidence on its own — that is what the two probes in step 6
are for. If a command failed, the stage failed, whatever you think of the reason.

## The run

Work in `.ventis-tests/<slug>/`, with the clone at `src/` and every command's
output written into `artifacts/`.

```bash
mkdir -p .ventis-tests/<slug>/artifacts
git clone --depth 1 <repo> .ventis-tests/<slug>/src
git -C .ventis-tests/<slug>/src rev-parse HEAD          # repo_sha
git rev-parse HEAD:.claude/skills/porting-to-ventis     # skill_sha
git rev-parse HEAD:ventis                               # ventis_sha
```

| # | `farthest_step` | What runs |
| --- | --- | --- |
| 1 | `fetched` | the clone above |
| 2 | `screened` | your read of the repo — see below |
| 3 | `wired` | write `.env` beside the source with the keys the repo needs |
| 4 | `ported` | **the `porting-to-ventis` skill**, on this repo |
| 5 | `validated` | `python .claude/skills/porting-to-ventis/validate.py .` |
| 6 | `built` | `ventis build -c config/global_controller.yaml`, then both probes |
| 7 | `deployed` | `ventis deploy -c config/global_controller.yaml`, backgrounded |
| 8 | `served` | `POST /main`, then poll `GET /status/<request_id>` |

`farthest_step` is the furthest stage reached. Step 5 is the exception: it does
**not** gate what follows.

### Step 2 — read the repo before spending anything on it

Answer these from the source. Each rejection below was learned by paying an
agent's full budget to rediscover it.

| Question | Reject when |
| --- | --- |
| Is there a module the adapter can import from the project root? | No root-level `.py` **and** no `pyproject.toml`/`setup.py`/`setup.cfg`. Without packaging metadata nothing is importable at `/app`. This is M24, and it rejects most tutorial repos. |
| Which provider will it actually call? | It needs one whose key you do not have. |
| Does it need something to reach? | It reads the address or credentials of a service you are not standing up. Ventis provides Redis; everything else is on you. |
| Is there Python at all? | Notebooks only, or no LLM call anywhere. |
| Is it small to medium? | Hundreds of modules, or a framework rather than a project. |

**Reading the imports is not enough to answer the provider question.** Every
LangGraph template reaches its model through `init_chat_model("anthropic/…")` or
a config default string, so a repo can depend entirely on Anthropic while
importing nothing named `anthropic` — and can import `langchain_openai` for its
embeddings while its chat model is Claude. Grep the string literals as well as
the imports, and take the union:

```bash
grep -rnoE '"(openai|anthropic|google_genai|bedrock|cohere|mistralai)[:/][^"]+"' <src>
```

A repo needing a provider you cannot serve is `blocked`, not `failed`. Record
which provider and stop — that count is the argument for obtaining the key.

**Ask the backing-service question as a principle, not as a list.** Enumerating
prefixes reads as a checklist and lets everything unlisted through: a list
naming `ELASTICSEARCH_*` and `PINECONE_*` passed a repo whose first node calls
`int(os.getenv("SSH_PORT"))` against a remote host that does not exist. Read the
env vars the source actually reads, and for each one ask **what would have to be
running for this to work**:

```bash
grep -rhoE "getenv\(\s*[\"'][A-Z_]{3,}|environ\[[\"'][A-Z_]{3,}" <src> \
  | grep -oE "[A-Z_]{3,}" | sort -u
```

An LLM key you hold is fine. A host to SSH into, a vector store, a database, a
search API, an object store — anything the source must connect to and you are
not providing — is out of scope. A repo whose work is *reaching* such a service
stays out of scope even when a port of it builds and serves: the request returns
the source's own failure, and the run proves nothing about the skill.

### Step 3 — the credential goes beside the source, never inside it

Write `.env` at the project root with the real keys, and let the port's
`config/global_controller.yaml` point `env_file:` at it. Never bake a key into
the build context: `ventis build` sweeps the project into every image, and
`_sweep_project_files` skips dotfiles precisely so `.env` cannot ride along.

### Step 4 — run the skill

Use the `porting-to-ventis` skill on the clone. Follow it as written; it is the
artifact under test. When it tells you to report something rather than fix it,
write `PORT_REPORT.md` in the repo and stop — **that is the skill working, and
the run is `blocked`, not `failed`.** Those paths fire on things Ventis cannot
do, so the finding belongs in `core_issue`.

### Step 6 — build, then probe twice

```bash
ventis build -c config/global_controller.yaml

# 1. the runtime, which fails before your agent is reached
docker run --rm ventis-<name> python -c "import local_controller"

# 2. the agent, loaded the way _load_agent loads it
docker run --rm ventis-<name> python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('m', '<entrypoint basename>.py')
m = importlib.util.module_from_spec(spec); sys.modules['m'] = m
spec.loader.exec_module(m); m.<AgentName>(); print('ok')"
```

Probe 1 catches the protobuf/gRPC wall — a `core_issue`, since the fix belongs in
`generate_docker`. Probe 2 catches everything `_load_agent` swallows, which
otherwise surfaces only as `"No agent loaded"` at step 8.

### Step 8 — served means the port answered

`POST /main {"query": ...}` and poll `/status/<request_id>`. **Send a query the
repo can actually act on** — read its README first. Asking an SSH operations
agent about "animals" tests nothing.

An outer `"status": "done"` with an inner `"status": "failed"` means the port
worked and the project's own logic did not. That is `passed`, because the port
did what the skill promises — carry a request to the source and return the
source's own result — but say so in `analysis`. A bare missing env var
(`'ELASTICSEARCH_API_KEY'`) is `blocked`: nobody configured it.

## Deciding the status

| `status` | When |
| --- | --- |
| `passed` | Step 8 returned the source's own result. |
| `blocked` | Nothing was tested: out of scope at step 2, a missing key or backing service, or the skill correctly reported-and-stopped. |
| `failed` | The port was attempted and something about it did not work. |

`blocked` is not a soft `failed`. It means this repository produced no evidence
about the skill, and rows that produced no evidence must not be counted as if
they had.

## Recording

Always record, including for `blocked` runs — a rejection is the datum.

```bash
python .claude/skills/testing-porting-to-ventis/record.py \
    --db .ventis-tests/results.sqlite <<'JSON'
{
  "repo": "https://github.com/owner/name",
  "repo_sha": "…", "skill_sha": "…", "ventis_sha": "…",
  "stars": 128, "framework": "langgraph", "is_multiagent": 1,
  "description": "what it does, technically",
  "farthest_step": "built", "status": "failed", "validate_ok": 1,
  "core_issue":  [{"kind": "runtime_import", "detail": "…"}],
  "skill_issue": [{"kind": "no_fanout", "detail": "…"}],
  "analysis": "what happened and why, in a few sentences",
  "artifacts": ".ventis-tests/<slug>/artifacts",
  "started_at": "2026-08-28T00:00:00Z", "ended_at": "2026-08-28T00:10:00Z"
}
JSON
```

`core_issue` is what a Ventis owner must fix; `skill_issue` is what the skill
file must say better. Keep them apart — collapsing them loses the distinction the
whole exercise exists to produce. Leave both empty when the run had no findings.

## Common mistakes

| Mistake | What it costs |
| --- | --- |
| Screening on imports alone | Anthropic-only repos reach step 4 and burn a budget before failing |
| Skipping the `provider/model` grep | Same, and it is the default shape of every LangGraph template |
| Letting `validate.py` gate the build | The one case where a validation was wrong to block becomes unobservable |
| Treating `Build complete.` as evidence | A green build and a healthy replica are both compatible with a container that serves nothing |
| Running one probe instead of two | Probe 1's failure is a Ventis bug; probe 2's is the port's; neither covers the other |
| Scoring report-and-stop as `failed` | Counts the skill working as the skill failing, and buries the Ventis gap that caused it |
| Sending `{"query": "animals"}` to everything | `served` stops meaning anything |
| Screening backing services against a list of prefixes | Whatever is not on the list gets in — ask what must be running, not what matches |
| Editing the skill mid-corpus | The pass rate loses its denominator |
