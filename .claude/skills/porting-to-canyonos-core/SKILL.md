---
name: porting-to-canyonos-core
description: Ports existing LangChain, LangGraph, CrewAI, AutoGen, and hand-rolled Python agent projects onto CanyonOS Core, whose CLI is `ventis` and whose artifacts live in a `.car` directory. Writes the `.car/config` manifest and declarations, duplicates the source into `.car/app`, writes adapters and the workflow, then validates, builds, deploys and probes. Use when converting, migrating, adapting, packaging, building or deploying an existing agent or multi-agent project onto CanyonOS Core or ventis, when running `ventis build` or `ventis deploy`, or when a `.car` port fails to build, load an agent, or answer a request.
---

# Port an agent project to CanyonOS Core

Requires Python, Docker, and the `ventis` CLI. `validate.py` in this skill needs
Python 3 and `pyyaml`.

CanyonOS Core is the product name. Its compatibility executable and Python
package remain `ventis`; environment variables and Docker resources retain the
`VENTIS_*` and `ventis-*` prefixes. These are protocol identifiers, not branding
strings. Do not rename them.

## Port checklist

Copy this into your response and check items off as you go. Every step below
maps to one line here.

```
Port progress:
- [ ] 1. Copy the source into .car/app, rooted at its import root
- [ ] 2. Survey the copy and choose service boundaries
- [ ] 3. Write declarations, adapters, workflow, config
- [ ] 4. validate.py reports 0 errors
- [ ] 5. ventis build succeeds
- [ ] 6. Both images pass their probes
- [ ] 7. A real request returns through /status
- [ ] 8. Clean up; git status shows nothing outside .car
```

Do not skip step 4. Every failure mode it reports survives a green build and a
healthy replica, and then costs a deploy cycle to rediscover.

## Load references only when needed

- Read [references/packaging.md](references/packaging.md) when a source import
  does not resolve from `/app`, the source is nested, or packaging metadata is
  involved.
- Read [references/llm-proxy.md](references/llm-proxy.md) only when the target
  includes `llm_proxy`.
- Read [references/ec2.md](references/ec2.md) only when any config entry uses
  `provider: EC2`.
- Read [references/troubleshooting.md](references/troubleshooting.md) after a
  failed build, image probe, deploy, or request.
- Read [references/runtime-contract.md](references/runtime-contract.md) when a
  validator finding needs explanation or the runtime mechanism is unclear.
- Read [references/example-port.md](references/example-port.md) for one port end
  to end -- the decisions, the files, and the evidence that closed it.

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
lives in -- not into new `agents/` and `workflow/` directories. `ventis`
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

## 1. Duplicate the source, then survey it

Copy the application source into `.car/app/`, preserving its structure. Leave
out only what no container should carry: `.git/`, `.car/` itself, virtualenvs,
caches, build outputs, and `.env` files holding real credentials.

**Root the copy at the source's import root, which is not always its repository
root.** `/app` is the copy, and without the editable-install capability it is
the only entry on `sys.path`. A source whose modules import each other as
`from tools import ...` while living under `src/` has `src/` as its import
root: copy the *contents* of `src/` to `.car/app/`, or every one of those
imports raises `ModuleNotFoundError` inside `_load_agent` and the first
request answers `No agent loaded`. Read the source's own imports, not its
directory names, to decide. Re-rooting is free here in a way it never was
before: `.car/app` is a copy Canyon owns, so nothing in the developer's tree
moves.

```bash
mkdir -p .car/config
rsync -a --exclude '.git' --exclude '.car' --exclude '.venv' --exclude 'venv' \
  --exclude '__pycache__' --exclude '.env' <import-root>/ .car/app/
```

Every edit from here on is inside `.car`. The application source outside it is
read-only for the rest of the port -- `git status` at the end shows `.car/` and
nothing else.

Then survey the copy. Identify:

1. The source entry point and callable input/output.
2. Framework-owned control flow (`StateGraph`, `Crew`, `GroupChat`, routing,
   `Send`, `Command`, interrupts).
3. Runtime-injected services nodes read: stores, context, memory, sessions, or
   callback managers.
4. Sync versus async boundaries.
5. Imports and declared runtime dependencies.
6. Model provider, credential names, streaming use, and optional `llm_proxy`.
7. Whether independent work fans out and benefits from separate replicas.
8. Whether source imports resolve from `.car/app`, the root that becomes
   `/app`. This is the check the validator turns into V031, and it is the one
   most likely to survive a green build and a healthy replica.

Run the validator now, and again after every change until it reports 0 errors.
Execute it; do not read it. Its header detects capabilities directly from the
importable runtime rather than from release history:

```bash
python <skill_dir>/validate.py .car
```

If config or agent yaml is malformed, the validator defers to `ventis build`.
Capability-gated findings say which runtime behavior is available.

## 2. Choose service boundaries

Start with one service. Split only when it creates independent parallel work or
a distinct resource/replica profile.

- Keep a ReAct loop together; every turn needs shared message history.
- Hoist supervisor task lists and `Send`-style fan-out into the workflow.
- Do not create a one-replica service with no distinct resource profile merely
  to mirror every source graph node.

Rewrite framework-owned edges as ordinary Python. Import the connected node
functions unchanged. Construct runtime-injected service objects from source
configuration; do not invent models, dimensions, stores, or defaults silently.
Report any choice the source does not specify.

## 3. Write declarations and adapters

### Agent yaml

Declarations go in `.car/config/`, beside the manifest. The build reads every
`*.yaml` there and keeps the ones with a top-level `agent.name`, so the
manifest and `policy.yaml` drop out on their own.

Use one yaml per deployed service. Argument types are bare builtins only:
`str`, `int`, `float`, `bool`, `dict`, or `list`. Every declared argument is
required by the generated stub. `returns.type` is documentation; use `dict` or
`list` to signal that workflow callers must `json.loads` the returned string.

### Adapter

Write the adapter where the code it wraps already lives, and give the module a
name of its own -- two agents may not share one entrypoint, because each
agent's stub is written over its own entrypoint and the second would land on
the first. Prefer editing the copied module in place over adding a parallel
one; that is what the copy is for.

The entrypoint exposes a module-level class named exactly `agent.name`. It
constructs with no arguments and its declared methods are synchronous. Read
configuration from the environment in `__init__`. Bridge source coroutines
inside a synchronous method with `asyncio.run(...)`. Serialize framework objects
with their own JSON-safe serializer before returning.

Do not duplicate source prompts, tools, schemas, or model calls. Keep the source
provider and SDK.

### Workflow

Expose `main(query: str)` and call `deploy(main, port=...)` at module scope.
Import each agent from its own `entrypoint`, exactly where the source copy
keeps it -- that is the one module the build replaces with a stub:

```python
from deploy import deploy
from <package>.<module> import <AgentName>   # the agent's entrypoint path
```

Any other route to the class -- a flat name, a package re-export, a second
copy of the module -- reaches the real class and runs the agent in the workflow
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

For each service, keep these names aligned:

```text
config entry name == yaml agent.name == entrypoint class name
```

`entrypoint` and `workflow_file` are relative to `.car/app/` and must stay
inside it. Use lowercase `provider: local`; `replicas` is an integer;
`requirements` is a list of distribution-name strings. Put `env_file` at config top level when the
runtime capability is available. Omit `policy.yaml` unless access must be
restricted; if present, give it a non-empty `rules` list.

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
| M6 | Config names MUST match yaml agent names | build |
| M7 | Config names MUST not collide after lowercase normalization | build output |
| M8 | Local provider MUST be lowercase `local` | deploy preflight |
| M9 | `replicas` MUST be an integer | deploy preflight |
| M10 | `requirements` MUST be a list of strings | build |
| M11 | Workflow MUST expose `main(query)`; extra parameters MUST default | V016 |
| M12 | Workflow MUST NEVER contain a main guard | V017 |
| M13 | Fan-out MUST dispatch all calls before resolving any | V018 |
| M14 | A module at the root of the copy MUST not take a runtime flat name | V019 |
| M15 | Workflow MUST import each agent from its own `entrypoint` module | V023 |
| M16 | Policy MUST be absent or contain a non-empty `rules` list | deploy preflight |
| M17 | EC2 entries MUST satisfy the EC2 deployment contract | deploy preflight |
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

## 4. Validate, build, and probe

Run static preflight, then let the build own build-time validation. Both run
from the application root:

```bash
python <skill_dir>/validate.py .car
ventis build
```

Fix every ERROR and re-run the validator until it reports 0 errors before
running `ventis build`. A build that skips this passes, and the port then fails
at `docker run` or on the first request, where the message names a container
rather than the mistake.

A green build never imports the adapter. Probe each agent image in this order:

```bash
# Runtime startup path
 docker run --rm ventis-<agent-name-lowercased> \
   python -c "import local_controller"

# Agent load path; the entrypoint keeps its path from the source copy.
# Include --env-file when configured.
 docker run --rm --env-file <env-file> ventis-<agent-name-lowercased> \
   python -c "import importlib.util,sys; \
s=importlib.util.spec_from_file_location('m','<entrypoint-path>'); \
m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m); \
m.<AgentName>();print('ok')"
```

Also probe the workflow image with `python -c "import local_controller"`; it has
its own dependency resolve and generated-stub imports.

Then deploy, send a representative request, and poll its status:

```bash
ventis deploy
curl -X POST http://localhost:8080/main \
  -H 'Content-Type: application/json' -d '{"query":"<real input>"}'
curl http://localhost:8080/status/<request_id>
```

A successful outer request with a source-level failure still proves the port
reached and returned the source behavior. Record the distinction.

## 5. Clean up

After collecting evidence, stop foreground deploy with Ctrl+C and wait for
controller cleanup. Remove exact leftovers if startup crashed. Then remove build
products and exact images from this config:

```bash
ventis clean
docker image rm ventis-<agent-name-lowercased> \
  ventis-<workflow-entry-name-lowercased>

test ! -e .car/stubs && test ! -e .car/grpc_stubs && test ! -e .car/docker_container
docker ps -a --format '{{.Names}}'
```

`ventis clean` removes only `.car/stubs/`, `.car/grpc_stubs/`, and
`.car/docker_container/`; it does not remove containers or images. Keep
`.car/config`, `.car/app`, and requested logs or reports.

Finally, confirm the decoupling held: `git status` outside `.car` reports no
change to any file the developer owns.
