---
name: porting-to-canyonos-core
description: Ports existing LangChain, LangGraph, CrewAI, AutoGen, and hand-rolled Python agent projects to CanyonOS Core. Use when converting, migrating, adapting, packaging, building, or deploying an existing agent or multi-agent project onto CanyonOS Core.
compatibility: Requires Python, Docker, and the `canyonos` compatibility CLI. Runtime identifiers remain `canyonos`, `CANYONOS_*`, and `canyonos-*`.
---

# Port an agent project to CanyonOS Core

CanyonOS Core is the product name. Its compatibility executable and Python
package remain `canyonos`; environment variables and Docker resources retain the
`CANYONOS_*` and `canyonos-*` prefixes. These are protocol identifiers, not branding
strings. Do not rename them.

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

## Goal: thin scaffolding beside untouched source

```text
agents/<name>.yaml               one callable surface per service
agents/<name>.py                 one thin adapter per service, when needed
workflow/<name>_workflow.py      HTTP entry point; calls deploy()
config/global_controller.yaml    deployment manifest
config/policy.yaml               optional access restriction
pyproject.toml                   conditional nested-import scaffolding
<source tree>                    unchanged
```

The file count follows the deployment. A multi-agent port has one yaml/adapter
pair per service that is worth deploying separately. If a source class already
satisfies the runtime contract, point its config entry at that file and do not
copy it into an adapter.

Everything the source already owns—prompts, tools, schemas, parsing, retries,
model clients, and node bodies—is imported. The port re-expresses only the
CanyonOS Core boundary and framework-owned orchestration.

The port root is the existing repository root and the directory from which
`canyonos build` runs. Write scaffolding there beside existing directories. If the
repository already uses `src/`, leave it in place and put `agents/`, `workflow/`,
and `config/` beside it. Never move or copy the repository into a new `src/`
directory, and never create an outer wrapper merely for the port.

## 1. Survey before writing

Identify:

1. The source entry point and callable input/output.
2. Framework-owned control flow (`StateGraph`, `Crew`, `GroupChat`, routing,
   `Send`, `Command`, interrupts).
3. Runtime-injected services nodes read: stores, context, memory, sessions, or
   callback managers.
4. Sync versus async boundaries.
5. Imports and declared runtime dependencies.
6. Model provider, credential names, streaming use, and optional `llm_proxy`.
7. Whether independent work fans out and benefits from separate replicas.
8. Whether source imports resolve from the project root that becomes `/app`.

Run the validator once now. Its header detects capabilities directly from the
importable runtime rather than external development metadata:

```bash
python <skill_dir>/validate.py .
```

If config or agent yaml is malformed, the validator defers to `canyonos build`.
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

Use one yaml per deployed service. Argument types are bare builtins only:
`str`, `int`, `float`, `bool`, `dict`, or `list`. Every declared argument is
required by the generated stub. `returns.type` is documentation; use `dict` or
`list` to signal that workflow callers must `json.loads` the returned string.

### Adapter

The entrypoint exposes a module-level class named exactly `agent.name`. It
constructs with no arguments and its declared methods are synchronous. Read
configuration from the environment in `__init__`. Bridge source coroutines
inside a synchronous method with `asyncio.run(...)`. Serialize framework objects
with their own JSON-safe serializer before returning.

Do not duplicate source prompts, tools, schemas, or model calls. Keep the source
provider and SDK.

### Workflow

Expose `main(query: str)` and call `deploy(main, port=...)` at module scope.
Import generated stubs by yaml basename and agent class name:

```python
from deploy import deploy
from agents.<yaml_basename> import <AgentName>
```

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

Use lowercase `provider: local`; `replicas` is an integer; `requirements` is a
list of distribution-name strings. Put `env_file` at config top level when the
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
| M14 | Project modules MUST not take runtime or generated-stub flat names | V019/V020 |
| M15 | Workflow MUST import stubs from `agents.<basename>` | V023 |
| M16 | Policy MUST be absent or contain a non-empty `rules` list | deploy preflight |
| M17 | EC2 entries MUST satisfy the EC2 deployment contract | deploy preflight |
| M18 | NEVER copy source prompts, tools, schemas, or model calls | review |
| M19 | NEVER hardcode or bake a real credential into an image | W003 |
| M20 | NEVER edit or vendor the source tree | `git status` |
| M21 | NEVER swap the source LLM provider | review |
| M22 | NEVER silently move, drop, or reclassify source dependencies | review |
| M23 | Framework control flow MUST be rewritten; source node logic MUST be imported | review |
| M24 | A non-resolving source import MUST have usable root packaging metadata when editable install is supported | V031 |

## 4. Validate, build, and probe

Run static preflight, then let the build own build-time validation:

```bash
python <skill_dir>/validate.py .
canyonos build -c config/global_controller.yaml
```

A green build never imports the adapter. Probe each agent image in this order:

```bash
# Runtime startup path
 docker run --rm canyonos-<agent-name-lowercased> \
   python -c "import local_controller"

# Agent load path; include --env-file when configured
 docker run --rm --env-file <env-file> canyonos-<agent-name-lowercased> \
   python -c "import importlib.util,sys; \
s=importlib.util.spec_from_file_location('m','<entrypoint-basename>.py'); \
m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m); \
m.<AgentName>();print('ok')"
```

Also probe the workflow image with `python -c "import local_controller"`; it has
its own dependency resolve and generated-stub imports.

Then deploy, send a representative request, and poll its status:

```bash
canyonos deploy -c config/global_controller.yaml
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
canyonos clean
docker image rm canyonos-<agent-name-lowercased> \
  canyonos-<workflow-entry-name-lowercased>

test ! -e stubs && test ! -e grpc_stubs && test ! -e docker_container
docker ps -a --format '{{.Names}}'
```

`canyonos clean` removes only `stubs/`, `grpc_stubs/`, and `docker_container/`; it
does not remove containers or images. Keep port scaffolding, untouched source,
and requested logs or reports.
