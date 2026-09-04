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
- [ ] 4. validate.py reports 0 errors; report readiness and stop
```

Do not skip step 4. The porting workflow ends when validation reports 0 errors:
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
python <skill_dir>/prepare.py <import-root> .car
```

The script creates `.car/config/` and copies the import root's **contents** into
`.car/app/`, preserving its structure. It excludes version-control data,
`.car`, virtualenvs, caches, build outputs, bytecode, and credential-bearing
`.env*` files while retaining `.env.example`, `.env.sample`, and
`.env.template`. It refuses to merge into an existing `.car/app`; use `--force`
only when intentionally replacing the entire source copy. `--force` leaves an
existing `.car/config/` unchanged.

Choosing the import root remains a porter decision; the script standardizes
only directory creation and copying. After it runs, every edit is inside
`.car`. The application source outside it is read-only for the rest of the port
-- `git status` at the end shows `.car/` and nothing else.

Then survey the copy. Identify:

1. The source entry point and callable input/output. When the repository has
   several plausible implementations, trace the imports from the production
   route, CLI, or documented launch path; do not choose the most convenient
   graph by filename.
2. Framework-owned control flow (`StateGraph`, `Crew`, `GroupChat`, routing,
   `Send`, `Command`, interrupts).
3. Runtime-injected services nodes read: stores, context, memory, sessions, or
   callback managers.
4. Sync versus async boundaries.
5. Imports and declared runtime dependencies.
6. Model provider, credential names, streaming use, and optional `llm_proxy`.
7. Whether independent work fans out and benefits from separate replicas.
8. Whether source imports resolve from `.car/app`, the root that becomes
   `/app`. This is the check the validator turns into V031.
9. Every non-Python file reached at runtime: prompts, CrewAI
   `agents.yaml`/`tasks.yaml`, PDFs, templates, schemas, and local corpora. If
   `sweeps_all_files` is unavailable, these are runtime blockers even though
   `prepare.py` copied them. Do not invent base64 embedding or rewrite hardcoded
   paths; report and stop.
10. Whether every Python file on the selected import graph parses. A syntax
    error already present in the source is a source defect, not permission to
    repair behavior silently. Report it and obtain approval before fixing only
    the `.car/app` copy.
11. Suspicious module-level behavior before any import or execution: obfuscated
    payloads, network downloads, shell/process calls, credential harvesting, or
    destructive filesystem operations. Stop and ask the user when found; do not
    import, build, or deploy untrusted code merely because it is a port target.

Run the validator now, and again after every change until it reports 0 errors.
Execute it; do not read it. Its header detects capabilities directly from the
importable runtime rather than from release history:

```bash
python <skill_dir>/validate.py .car
```

If config or agent yaml is malformed, the validator defers that failure to the
build phase inside a later `canyonos deploy`. Capability-gated findings say
which runtime behavior is available.

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

### Adapter

Write the adapter where the code it wraps already lives. *Which* module
`entrypoint` then names is the decision adapter.md gates: the build writes that
agent's stub over that path in every other image, so it is the one module in
the copy the port destroys.

The entrypoint exposes a module-level class named exactly `agent.name`. It
constructs with no arguments and its declared methods are synchronous. Read
configuration from the environment in `__init__`. Serialize framework objects
with their own JSON-safe serializer before returning.

Do not duplicate source prompts, tools, schemas, or model calls. Keep the source
provider and SDK.

### Workflow

Expose `main(query: str)` and call `deploy(main, port=...)` at module scope.
Import each agent from its own `entrypoint`, exactly where the source copy keeps
it -- that is the one module the build replaces with a stub:

```python
from deploy import deploy
from <package>.<module> import <AgentName>   # the agent's entrypoint path
```

Any other route to the class -- a flat name, a package re-export, a second copy
of the module -- reaches the real class and runs the agent in the workflow
process with none of the deployment behind it. That import needs no rewriting
when the source already imported the agent from there.

The deployment platform sends `{query: string}` to `/main`. Pack richer input
inside `query`; any additional workflow parameter has a default.

Dispatch every remote call before resolving any future:

```python
futures = [agent.work(item=item) for item in items]
results = [json.loads(future.value()) for future in futures]
```

Do not fuse dispatch and `.value()` in one comprehension; that silently
serializes fan-out. Do not add an `if __name__ == "__main__":` block: the
workflow is executed with `__name__ == "__main__"` in production.

### Config

`entrypoint` and `workflow_file` are relative to `.car/app/` and may not escape
it. `provider` is lowercase `local`, `replicas` is an integer, and
`requirements` is a per-entry list of distribution names -- the source's own
`requirements.txt` is never installed into any image. Omit `policy.yaml` unless
access must be restricted; if present, give it a non-empty `rules` list.
manifest.md carries the complete manifest and how to build each `requirements`
list.

Part of the manifest is derived from the copy; the rest is the developer's
deployment choice, and no reading of the source produces it. Derive what the
source decides. Do not derive what it does not.

Use the interaction exposed by `cli/canyonos/config.py` as the configuration UX.
Before writing `.car/config/global_controller.yaml`, present the same two
choices -- **View** and **Change** -- rather than silently choosing deployment
settings:

1. Build the complete candidate manifest in memory from derived values plus the
   defaults in manifest.md's ownership table.
2. **View** prints the whole candidate, not a summary. Clearly annotate defaults
   and values constrained by the source, such as `replicas: 1` for stateful
   in-memory services.
3. **Change** asks, in one batch, only for developer-owned values: provider and
   its EC2 block, replicas that are not constrained, resources, ports, secret
   file location, and whether access needs restricting. Show the current/default
   value for every choice. Apply the answers and show the resulting manifest.
4. Write the reviewed candidate, then run the validator.

If the coding environment can launch an interactive command, prefer running
`canyonos config` for this review. Otherwise reproduce its View/Change flow in
the conversation; do not skip the review merely because the CLI has no TTY.
Do not ask the developer for derived values such as `entrypoint` or
`requirements`: walking the copied source gives a more reliable answer.

Never block an unattended port on this interaction. `canyonos integrate`
launches this skill from a prompt, and a corpus run has no one at the terminal.
If no answer is available, write the displayed defaults, name them in the final
report, and keep going. The one exception is a value with no safe default:
`provider: EC2` needs infrastructure identifiers that are wrong to invent, so
an unanswered EC2 choice leaves the entry `local` and says so.

## Hard rules

Capitalized **MUST** and **NEVER** are reserved for port-breaking or
source-integrity rules. The owner column states where each is decided.

| ID | Rule | Owner |
|---|---|---|
| M1 | Entrypoint MUST define a class named exactly `agent.name` | V006 |
| M2 | The class MUST construct with no arguments | V007 |
| M3 | yaml argument names MUST match Python parameter names | V008 |
| M4 | yaml argument types MUST be bare builtins | V010 |
| M5 | Declared adapter methods MUST be synchronous | V009 |
| M6 | Config names MUST match yaml agent names | `canyonos deploy` build phase |
| M7 | Config names MUST not collide after lowercase normalization | `canyonos deploy` build output |
| M8 | Local provider MUST be lowercase `local` | `canyonos deploy` preflight |
| M9 | `replicas` MUST be an integer | `canyonos deploy` preflight |
| M10 | `requirements` MUST be a list of strings | `canyonos deploy` build phase |
| M11 | Workflow MUST expose `main(query)`; extra parameters MUST default | V016 |
| M12 | Workflow MUST NEVER contain a main guard | V017 |
| M13 | Fan-out MUST dispatch all calls before resolving any | V018 |
| M14 | A module at the root of the copy MUST not take a runtime flat name | V019 |
| M15 | Workflow MUST import each agent from its own `entrypoint` module | V023 |
| M16 | Policy MUST be absent or contain a non-empty `rules` list | `canyonos deploy` preflight |
| M17 | EC2 entries MUST satisfy the EC2 deployment contract | `canyonos deploy` preflight |
| M18 | NEVER copy source prompts, tools, schemas, or model calls | review |
| M19 | NEVER hardcode or bake a real credential into an image | W003 |
| M20 | NEVER write outside `.car`; the application source stays untouched | `git status` |
| M21 | NEVER swap the source LLM provider | review |
| M22 | NEVER silently move, drop, or reclassify source dependencies | review |
| M23 | Framework control flow MUST be rewritten; source node logic MUST be imported | review |
| M24 | A non-resolving source import MUST have usable packaging metadata at the root of the copy when editable install is supported | V031 |
| M25 | Two agents MUST NOT share one `entrypoint` | V020 |
| M26 | `.car` MUST hold `config/` beside `app/`, the source copy | V032 |
| M27 | `.car/app` MUST be rooted at the source's import root | V031 |
| M28 | `requirements` MUST cover every distribution that entry's import graph reaches, transitively | W006, dependency review |
| M29 | The entrypoint MUST NOT be a module another image imports for its real contents | adapter.md review |
| M30 | The entrypoint's package `__init__.py` MUST NOT re-export from it | V033 |
| M31 | Every segment of the `entrypoint` path MUST be a Python identifier | V034 |
| M32 | The entrypoint's own imports MUST be absolute | V035 |

## 4. Validate and stop

Run static preflight from the application root:

```bash
python <skill_dir>/validate.py .car
```

Fix every ERROR and re-run until it reports 0 errors. Warnings and capability
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

Report build or deployment failures without silently changing source behavior,
dependencies, provider, or deployment settings. `canyonos deploy` follows the
controller logs; Ctrl+C stops log monitoring, not necessarily the deployment.
Use `canyonos stop` only when the user asks to stop it.
