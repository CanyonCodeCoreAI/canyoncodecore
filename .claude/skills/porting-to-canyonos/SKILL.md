---
name: porting-to-canyonos
description: Ports existing LangChain, LangGraph, CrewAI, AutoGen, and hand-rolled Python agent projects onto CanyonOS Core, whose CLI is `canyonos` and whose artifacts live in a `.car` directory. Writes `.car/config`, copies source into `.car/app`, writes adapters and the workflow, and validates the port. Stops after validation and asks before running `canyonos deploy`, which performs both build and deployment. Use when converting, migrating, adapting, packaging, validating, or deploying an existing agent or multi-agent project onto CanyonOS Core, or when a `.car` port fails validation, build, load, or deployment.
---

# Port an agent project to CanyonOS Core

Requires Python, Docker, and the `canyonos` CLI. `prepare.py` uses only the
Python standard library; `validate.py` needs Python 3 and `pyyaml`.

CanyonOS Core is the product name and `canyonos` is its user-facing CLI. The
internal compatibility Python package, environment variables, and Docker
resources retain the `ventis`, `VENTIS_*`, and `ventis-*` names. These are
protocol identifiers, not CLI instructions or branding strings. Do not rename
them, and do not tell users to run the obsolete `ventis` CLI.

## Port checklist

Copy this into your response and check items off as you go. Every step below
maps to one line here.

```
Port progress:
- [ ] 1. Choose the import root; run prepare.py to create .car/config and .car/app
- [ ] 2. Survey the copy and choose service boundaries
- [ ] 3. Read adapter.md + manifest.md; write declarations, adapters, workflow;
         use the `canyonos config` flow to review deployment choices, then write config
- [ ] 4. validate.py exits 0; report readiness and stop
```

Do not skip step 4. The porting workflow ends when validation exits 0:
report the files created, warnings and unresolved runtime blockers, then stop.
Never build or deploy as an implicit continuation of the port.

## References

Every reference is linked from here and read whole when its trigger fires. What
differs between the groups is the *kind* of trigger.

**Before you write.** Triggered by the step, not by a symptom: a porter cannot
look up a rule whose violation builds green and fails in a container. Neither is
optional.

- [references/adapter.md](references/adapter.md) -- choosing the entrypoint,
  bridging async, session state. Read before writing into `.car/app`.
- [references/manifest.md](references/manifest.md) -- the agent yaml, the
  complete manifest, and how to build a `requirements` list. Read before writing
  into `.car/config`.

**When the target has this shape.** Triggered by a fact about the source or the
deployment, all three knowable at step 1.

- [references/packaging.md](references/packaging.md) -- read when a source
  import does not resolve from `/app`, the source is nested, packaging metadata
  is involved, or the source reads non-Python files at runtime.
- [references/source-survey.md](references/source-survey.md) -- read after
  preparing the copy and before choosing service boundaries.
- [references/refresh.md](references/refresh.md) -- read when `.car/app` already
  exists and the source has changed; preserve port edits while refreshing it.
- [references/llm-proxy.md](references/llm-proxy.md) -- read when the target
  includes `llm_proxy`.
- [references/ec2.md](references/ec2.md) -- read when any config entry uses
  `provider: EC2`.

**After something failed.** Triggered by a symptom.

- [references/troubleshooting.md](references/troubleshooting.md) -- read after an
  explicitly approved deploy fails during build, startup, or a request;
  symptom-to-cause tables.
- [references/runtime-contract.md](references/runtime-contract.md) -- read when
  a validator finding needs explanation or the runtime mechanism is unclear.

**For orientation.**

- [references/example-port.md](references/example-port.md) -- one LangGraph port
  end to end: the decisions, the files, and the evidence that closed it.

## Goal: a self-contained `.car`, and a source tree that never learns about it

The port lives entirely inside `.car/`, next to the application source and
never inside it:

```text
.car/config/global_controller.yaml   deployment manifest
.car/config/policy.yaml              optional access restriction
.car/config/<name>.yaml              one callable surface per service
.car/app/                            a copy of the application source
.car/app/<dir>/<name>.py             the adapter, written where the code it wraps lives
.car/app/<dir>/<name>_workflow.py    HTTP entry point; calls deploy()
.car/app/pyproject.toml              conditional nested-import scaffolding
<application source>/                the developer's tree, untouched and unaware
```

`.car` has exactly two authored directories: `config/`, which holds every
declaration Canyon owns, and `app/`, the copy that becomes `/app` in every
container. The container keeps the directory structure the application already
had. Write adapters into that copy, in the module the code they wrap already
lives in -- not into new `agents/` and `workflow/` directories. `canyonos`
commands run from the application root and read `.car` below it.

Nothing under `.car` points back out at the application source, and nothing in
the application source points at `.car`. Deleting `.car` returns the project to
exactly where it started; regenerating it touches no file the developer owns.

The file count follows the deployment. A multi-agent port has one yaml/adapter
pair per service that is worth deploying separately. If a source class in the
copy already satisfies the runtime contract, point its config entry at that
file and do not write an adapter beside it.

Everything the source already owns—prompts, tools, schemas, parsing, retries,
model clients, and node bodies—is imported from where the copy keeps it. The
port re-expresses only the CanyonOS Core boundary and framework-owned
orchestration.

## 1. Prepare the artifact tree, then survey it

**Choose the source's import root, which is not always its repository root.**
`/app` is the copy, and without the editable-install capability it is the only
entry on `sys.path`, so a source under `src/` that imports `from tools import
...` needs the *contents* of `src/` at `.car/app/`. Read the source's own
imports, not its directory names, to decide. Getting it wrong builds green and
answers `No agent loaded` on the first request;
[references/packaging.md](references/packaging.md) works the case through.

Once the import root is known, use the skill's preparation script rather than
assembling `.car` with ad hoc copy commands:

```bash
python3 <skill_dir>/prepare.py <import-root> .car
```

The script creates `.car/config/` and copies the import root's **contents** into
`.car/app/`, preserving its structure. It excludes version-control data,
`.car`, virtualenvs, caches, build outputs, bytecode, and credential-bearing
`.env*` files while retaining `.env.example`, `.env.sample`, and
`.env.template`. It rejects symbolic links because they either escape the
self-contained artifact or are skipped by the runtime source sweep.

If `.car/app` already exists, read [references/refresh.md](references/refresh.md)
and use `--refresh`. It updates source-owned files while preserving port edits,
and stops atomically when both sides changed one path. Use `--force` only to
discard every edit in `.car/app`; it leaves `.car/config/` unchanged.

Choosing the import root remains a porter decision; the script standardizes
only directory creation and copying. After it runs, every edit is inside
`.car`. The application source outside it is read-only for the rest of the port
-- `git status` at the end shows `.car/` and nothing else.

Read [references/source-survey.md](references/source-survey.md), survey the
copy, and run the validator. The survey determines the source facts used in the
next two steps; do not infer them from framework conventions.

## 2. Choose service boundaries

Start with one service. Split only when it creates independent parallel work or
a distinct resource/replica profile.

- Keep a ReAct loop together; every turn needs shared message history.
- Hoist supervisor task lists and `Send`-style fan-out into the workflow.
- Do not create a one-replica service with no distinct resource profile merely
  to mirror every source graph node.

Rewrite framework-owned edges as ordinary Python **only where they cross a
service boundary you chose**. A graph whose nodes all land in one agent has no
boundary to express: keep `graph.compile().invoke(...)` and wrap it. Rewriting
it anyway restates control flow the source already had working and buys no
deployment. Import the connected node functions unchanged wherever you do
rewrite.

Construct runtime-injected service objects from source configuration; do not
invent models, dimensions, stores, or defaults silently. Report any choice the
source does not specify. A service object that holds state across requests -- a
vector store, a memory, a checkpointer built in `__init__` -- makes
`replicas: 1` a correctness requirement rather than a sizing choice, because
the controller picks a replica per call and the others cannot see that state.
Say so in the report; do not leave it implied.

## 3. Write declarations and adapters

Read [references/adapter.md](references/adapter.md) before writing an adapter,
and [references/manifest.md](references/manifest.md) before writing
`.car/config`. Neither is optional and neither is triggered by a symptom: every
rule in them builds green and fails inside a container.

For each service, keep these names aligned:

```text
config entry name == yaml agent.name == entrypoint class name
```

Write a no-argument, synchronous adapter class at the entrypoint selected by
`adapter.md`. Import source-owned behavior instead of duplicating it. Expose
`main(query: str)` in the workflow, import every service from its exact
entrypoint module, and call `deploy(main, port=...)` at module scope.

For parallel remote calls, dispatch before resolving:

```python
futures = [agent.work(item=item) for item in items]
results = [json.loads(future.value()) for future in futures]
```

Do not fuse dispatch and `.value()` in one comprehension. Do not add a main
guard; the workflow executes as `__main__` in production.

Build declarations and per-image requirements from the copied import graph.
Then use the View/Change flow in `manifest.md` (and `canyonos config` when
interactive) to review developer-owned deployment choices. Write only the
reviewed candidate and rerun validation.

## Source-integrity boundary

The validator owns mechanical runtime rules; do not duplicate its check list in
the prompt. The porter owns the rules static analysis cannot prove:

- Never edit outside `.car` or copy source-owned prompts, tools, schemas, model
  calls, and node bodies into an adapter.
- Never swap the source provider, invent runtime configuration, or silently
  move, drop, or reclassify a dependency.
- Rewrite framework control flow only where it crosses a service boundary;
  preserve it inside a service.
- Never hardcode or bake a real credential into `.car`.

When a source defect or unsupported runtime capability requires breaking one of
these boundaries, report the blocker and obtain approval for that specific
change. Do not broaden approval to unrelated source edits.

## 4. Validate and stop

Run static preflight from the application root:

```bash
python3 <skill_dir>/validate.py .car
```

Fix every ERROR and re-run until it exits 0. Warnings and capability
limitations are not permission to hide risk: list each one in the handoff and
say whether it blocks this source. Confirm that `git status` outside `.car`
shows no change to a file the developer owns.

At that point, report that the `.car` port is validated and stop. Ask the user a
direct yes/no question before taking the next step:

> Validation passed. Run `canyonos deploy` now? This will build images and start
> the deployment.

Do not run a standalone build first. `canyonos deploy` owns both build and
deployment, and must run only after explicit user approval. Silence, an
unattended run, or the original request to "port" is not approval.

If the user approves, run from the application root:

```bash
canyonos deploy
```

Do not add build, probe, deployment-debugging, or cleanup work to this skill's
porting flow.
