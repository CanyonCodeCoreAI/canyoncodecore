# skill_harness

Runs `porting-to-ventis` against a list of repositories and records how far each
one got. `DESIGN.md` is why it is shaped this way; this file is how to run it.

## Setup

```shell
uv venv --python 3.12 .venv
uv pip install -e .
export AWS_BEARER_TOKEN_BEDROCK=...       # required; stage 3 cannot run without it
export ANTHROPIC_API_KEY=...              # required; `claude --bare` reads only this
```

## Run

```shell
.venv/bin/python -m skill_harness run --repos skill_harness/repos.yaml
.venv/bin/python -m skill_harness report
```

Results land in `.harness/results.sqlite`; each repo's artifacts — the agent
trace, the four files it wrote, every stage's log — in `.harness/<slug>/artifacts/`.
Nothing is analysed at write time, so the aggregations come later, from those
directories.

## What each module does

| File | Stage | Job |
|---|---|---|
| `runner.py` | — | sequences the pipeline; concurrency 2, with 6–8 serialised |
| `screen.py` | 2 | reads the repo without running it; finds the hardcoded model ids |
| `shim.py` | 3 | rewrites the `model` field on the way to Bedrock; nothing else |
| `stages.py` | 1–8 | one function per stage, each writing its own log |
| `db.py` | — | schema, and `confusion()` — validate.py's accuracy |

## Two things to know before reading a result

**Stages 6–8 hold a global lock.** They build images tagged `ventis-<agent name>`,
bind the workflow's `api_port`, and `ventis deploy` starts its own Redis
container. Two repos cannot be in those stages at once no matter how wide
`--concurrency` is, so raising it past 2 buys less than it looks like it should.

**Stage 5 does not gate.** `validate.py` failing does not stop the build. That is
deliberate — it is the only way to find out that a validation was wrong to block —
so `farthest_step` can read `served` on a repo whose `validate_ok` is 0. Those
rows are the interesting ones. `report` prints the resulting confusion matrix.

## Not done yet

GitHub search and repo selection (the list is hand-written), retry policy, and
any aggregation over `artifacts/`. Deliberately — see `DESIGN.md` section 5.
