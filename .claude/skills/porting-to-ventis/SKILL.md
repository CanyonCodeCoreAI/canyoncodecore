---
name: porting-to-ventis
description: Use when porting an existing agent project (LangChain, LangGraph, CrewAI, AutoGen, or a hand-rolled pipeline) onto Ventis
---

# Porting an agent project to Ventis

## How to read the rules in this file

Set in capitals, **MUST** and **NEVER** mark a rule whose violation breaks the
port: the build skips an image, `ventis deploy` dies, or the first request
fails. Every one is indexed in [The MUST list](#the-must-list), and every one a
machine can decide is checked by `validate.py`. Nothing else in this file is
written in capitals, so `grep -nE '\b(MUST|NEVER)\b' SKILL.md` returns the
rules and only the rules.

Everything else is stated as fact, in the indicative — how Ventis behaves, and
what follows from it. There is no "should", and nothing is left to taste that
does not have to be.

## A port is four files beside an untouched source tree

```
agents/<name>.yaml               declares the callable surface
agents/<name>.py                 the thinnest class that satisfies Ventis
workflow/<name>_workflow.py      entry point; calls deploy()
config/global_controller.yaml    deployment manifest
config/policy.yaml               optional — only to restrict access
<the source tree>                NOT EDITED — copied whole into every image
```

The two `agents/` files share one basename, as every example does — the stub the
build generates lands where it cannot collide with either. Pick a basename that
is not a module the adapter imports.

Everything the source already does — prompts, tools, schemas, parsing, retries,
its LLM client — is reached with an `import`. **A port that contains a prompt
string, a tool body, or a model call that already exists in the source is a
rewrite of the project, not a port of it.**

Mechanism and evidence for every claim here: `ventis-contract.md`.
Symptom-to-cause lookup once something breaks: `traps.md`.

## Step 1 — Survey the source before writing anything

Ventis loads an agent by doing exactly this:

```python
module = <the file named by the config entry's `entrypoint`>
agent  = getattr(module, <yaml's agent.name>)()       # no arguments
result = getattr(agent, <the function name>)(**args)  # synchronous
```

Everything below is answerable by reading the source, and expensive to answer
after a green build.

### What the adapter fixes

Only the gap between that contract and what the source exposes. Nothing else
belongs in the file.

| The source exposes                                    | The adapter                                                          |
| ----------------------------------------------------- | ---------------------------------------------------------------------- |
| a no-argument class whose methods are synchronous     | none — point `entrypoint` at the file it already lives in            |
| module-level functions, or `@tool` objects (`StructuredTool` instances, not methods) | a class whose methods call them                                      |
| a compiled graph, a `Crew`, a `GroupChat`             | a class, plus the orchestration rewrite of Rule 1                    |
| `async def`                                           | a synchronous signature, with `asyncio.run(...)` inside the body     |
| framework objects as results (messages, graph state)  | the framework's own serializer — `json.dumps` runs on what you return |
| a model client built at import                        | nothing, once a credential can reach the container                   |

Most LangChain and LangGraph projects are rows 2–5, and none of those rows is a
reason to touch the source.

### What the config declares

The whole tree is copied into the image at its own relative paths, but the
container starts at `/app`, so only what landed flat imports on its own.

- **`requirements:`** on the config entry, a list of strings. It covers what the
  source imports beyond the runtime's own base list. A malformed value costs the
  whole list, not the one bad item: `_normalize_requirements` logs one warning
  and returns `[]`, and the build still succeeds with none of them installed.

- **The import root** — *needs a Ventis change that has no PR.* An editable
  install (`-e .`) driven by a `pyproject.toml`, `setup.py` or `setup.cfg` at the
  root is what makes a `src/` layout importable, and the project's own packaging
  metadata is what decides the root. `_install_step` exists only on
  `jiajunh/can-228-create-a-skill-to-convert-a-langchain-project-to-ventis`, which
  nobody has proposed merging. Until it lands, **only modules that land flat at
  `/app` import at all** — an adapter reaching into `src/pkg/` raises
  `ModuleNotFoundError` inside `_load_agent`, and the first request answers
  `"No agent loaded"`. `validate.py` probes for the feature and reports which
  rule is in force.

- **`env_file:`** — *needs PR #53, open against main.* A path relative to the
  project root pointing at a local `.env`, handed to every container as
  `docker run --env-file`. The file never enters the image, and `ventis deploy`
  fails on a bad path before launching anything. Without that PR, the only
  variables reaching a container are five `VENTIS_*` names, and a config that
  sets `env_file:` is setting a key nothing reads — the credential is silently
  dropped and the failure surfaces as a provider error on the first request.

**And one thing to report rather than fix.** Where the editable install exists,
`-e .` installs `[project.dependencies]` in the same resolve as `requirements:`,
and workshop projects routinely put their whole toolchain there so that one
install sets a laptop up. Compare each declared name against the source's
imports:

```bash
grep -rl "import <pkg>\|from <pkg>" <source dir>/
```

**Report the mismatch and stop there.** A grep finds names, not requirements: a
package loaded from a string at runtime is imported nowhere and still required.
Hand over the list, the cost (Step 4's protobuf wall, a full image build away),
and the two places entries can move to — `[project.optional-dependencies]`, which
`-e .` skips, and `[dependency-groups]`, which never enters package metadata at
all. Then let the owner decide, including deciding not to.

## Rule 1 — Rewrite orchestration, import everything else

One kind of source code genuinely cannot be reused: **control flow owned by a
framework runtime.** Ventis has no runtime to execute a LangGraph `StateGraph`, a
CrewAI `Crew` or an AutoGen `GroupChat`, so their wiring is re-expressed as
ordinary Python — in the workflow when it fans out, in the adapter when it does
not. The nodes those edges connected are imported, unchanged.

| Source code                                                | Treatment                      |
| ---------------------------------------------------------- | ------------------------------ |
| `StateGraph` / `add_edge` / `Send` / `Command(goto=...)`   | rewrite as Python control flow |
| `Crew(...)` / `GroupChat(...)` assembly                    | rewrite as Python control flow |
| node functions, prompts, tools, schemas, parsers, clients  | **import**                     |
| the source's model provider and SDK                        | **keep**                       |

## Rule 2 — Split only to scale

**Splitting into multiple agents is a scaling decision, not a format
requirement.** A single agent holding the whole pipeline is a valid Ventis
project. Start there, and hoist a loop into the workflow only when each iteration
fans out to more than one node:

- a single-agent ReAct loop **stays whole in one agent** — every turn needs the
  full message history, and hoisting pushes a growing message list through Redis
  each turn.
- a supervisor handing out N tasks, or a `Send` fan-out, is **hoisted** — N
  independent runs per request with no shared state is what replicas pay for.

An agent with `replicas: 1` and no distinct resource profile is a node Ventis
does nothing for. When you do split, say plainly what it buys.

## Step 2 — Write the files

**yaml** — argument `type` is pasted into an AST unchecked, so `str` `int`
`float` `bool` `dict` `list` are the whole vocabulary; the generated stub imports
nothing else, and anything that is not a builtin raises `NameError` when the stub
is imported. Every declared argument is required at every call site — the
generator emits no defaults. `returns` is read by nothing; its value is as a
marker, where `type: dict` tells whoever writes the workflow that this call site
needs `json.loads`.

**adapter** — the class name is `agent.name` and the constructor takes no
arguments: configuration comes from environment variables read in `__init__`.
What each method has to do is Step 1's table.

**workflow** — a top-level function named `main`, taking a single `query: str`,
plus `deploy(main, port=...)` at the end.

Ventis itself is permissive here: it serves `POST /<fn.__name__>` and splats the
request body in as kwargs, so any name and any arguments run. The deployment
platform's test endpoint is not. It posts to a hardcoded `/main`, and its body
schema is `{query: string}` under a strict validator, so a differently named
workflow is unreachable through it and any other key is rejected with 400 in the
control plane, before the request ever reaches the host. Pack richer input into
`query`; every other parameter needs a default, because nothing will ever send
it.

The file is `exec`'d rather than imported, so `__name__ == "__main__"` is true
and `if __name__ == "__main__":` blocks fire in production. `deploy()` blocks.

Dispatch every call before resolving any of them:

```python
futures = [agent.work(item=i) for i in items]        # returns immediately
results = [json.loads(f.value()) for f in futures]   # .value() blocks
```

Fused into one comprehension the calls run one after another. It does not error;
it is just silently serial, and the fan-out is gone.

**config** — each entry's `name` matches a yaml's `agent.name`, or the build
warns, skips that image, and still exits 0. Write `provider: local` in
**lowercase**: the port reservation compares `provider == "local"` with no
normalization, so `Local` leaves the port unreserved and deploy dies. `replicas`
is an integer — the list form that `_get_replica_placements` accepts raises
`TypeError` in `InstanceManager`.

**policy** — optional. Absent, every service is allowed. Present, it is read
strictly: an empty file, or a null `rules:`, is an `AttributeError` inside
`GlobalController.__init__` that kills `ventis deploy` before a container starts.
Write one only to restrict, and then remember that the first matching rule
decides — a service missing from the rule that matched is not a startup error but
an `Unauthorized` response after the request was accepted.

## The MUST list

Every hard rule in this file, and the check that decides it. `--` marks the ones
only a human can judge; they are the reason a clean validator run is a floor and
not a ceiling.

| #   | The rule                                                                  | Check      |
| --- | ------------------------------------------------------------------------- | ---------- |
| M1  | The entrypoint MUST define a class named exactly `agent.name`              | V006       |
| M2  | That class MUST construct with no arguments                                | V007       |
| M3  | A yaml `arguments[].name` MUST equal the Python parameter name exactly     | V008       |
| M4  | A yaml `type` MUST be a bare builtin                                       | V010       |
| M5  | A method backing a yaml function MUST be synchronous                       | V009       |
| M6  | Every config entry `name` MUST match some yaml `agent.name`                | V003, V005 |
| M7  | Two config entry `name`s MUST differ by more than case                     | V004       |
| M8  | `provider` MUST be lowercase `local` (EC2 takes any casing)                | V012       |
| M9  | `replicas` MUST be an integer                                              | V013       |
| M10 | `requirements:` MUST be a list of strings                                  | V014       |
| M11 | The workflow MUST expose `main(query)`; other parameters MUST have defaults | V015, V016 |
| M12 | The workflow MUST NEVER carry an `if __name__ == "__main__":` block        | V017       |
| M13 | A fan-out MUST dispatch every call before resolving any                    | V018       |
| M14 | No project module MUST take the flat name of a runtime file or a stub      | V019, V020 |
| M15 | `policy.yaml` MUST be absent, or MUST carry a non-empty `rules:` list      | V021       |
| M16 | An EC2 entry MUST declare `instance_type`, and `ec2:` MUST be complete     | V022       |
| M17 | NEVER copy a prompt, tool, or schema that exists in the source             | W001       |
| M18 | NEVER hardcode a credential, or ship one in the build context              | W003       |
| M19 | NEVER edit the source tree, and NEVER vendor it into `agents/`             | W002       |
| M20 | NEVER swap the LLM provider the source uses                                | --         |
| M21 | NEVER move or drop a declared dependency — report it and stop              | --         |
| M22 | Framework control flow MUST be rewritten; everything else MUST be imported | --         |

`validate.py` reports more than this list — V001, V002 and W005, W006 catch
files that do not parse and imports the container cannot satisfy — but every row
here has a check behind it.

Two more rules apply only where the Ventis you are targeting supports them,
which `validate.py` probes for rather than assumes:

| #   | The rule                                                          | Needs                   | Check |
| --- | ----------------------------------------------------------------- | ----------------------- | ----- |
| M23 | `env_file:` MUST resolve to a readable file, and MUST be the only way a credential enters | PR #53 | V030 |
| M24 | An adapter import from outside the project root MUST have packaging metadata behind it | no PR yet | V031 |

## Step 3 — Validate

```bash
python <skill_dir>/validate.py .
```

Run it before building. `ventis build` never imports your agent, and the
controller writes `healthy` to Redis *before* `_load_agent` runs — so a green
build and a healthy replica are both compatible with a container that can serve
nothing. This script is the only stage that reads what you actually wrote.

It parses; it never imports the port, so it is safe to run on a tree whose
dependencies are not installed. Errors are provable contract violations and exit
1. Warnings are the rewrite smells — M16, M17, M18 — and exit 0 on their own,
because a heuristic cannot be allowed to block a correct port; `--strict`
promotes them for CI. `--json` emits the findings as data.

The header prints which capability-gated rules are in force. A rule whose feature
is missing is reported `UNAVAILABLE`, never silently skipped.

## Step 4 — Build, then probe the image twice

`ventis build` prints `Build complete.` and tags every image for a project whose
container dies on startup. So run the image — tagged
`ventis-<agent.name lowercased>` — and do what the container does. **Both probes,
in this order. Neither covers the other.**

```bash
# 1. The runtime itself. This is what CMD runs, and it fails before your agent
#    is ever reached, so probing the entrypoint alone will miss it.
docker run --rm ventis-<name> python -c "import local_controller"

# 2. The agent, loaded the way _load_agent loads it.
docker run --rm ventis-<name> python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('m', '<entrypoint basename>.py')
m = importlib.util.module_from_spec(spec); sys.modules['m'] = m
spec.loader.exec_module(m); m.<AgentName>(); print('ok')"
```

Probe 1 exists because the gRPC stack is unpinned: `ventis build` runs
`grpc_tools.protoc` on the **host** and copies the generated `_pb2.py` in, where
a resolver that knows nothing about them picks the protobuf runtime. Protobuf
refuses gencode newer than its runtime, so a source whose dependencies hold
protobuf back kills the container on `import local_controller`. An image with few
requirements passes by coincidence. Report it — the fix belongs in
`generate_docker`, not in the port — and if Step 1 flagged declared-but-unimported
dependencies, name the culprit here.

Probe 2 exists because `_load_agent` catches every exception, logs it and returns
`None`: a missing dependency, a wrong class name, a constructor that wants
arguments, or a broken import inside the source tree are all invisible until the
first request answers `"No agent loaded"`.

Then `ventis deploy`, which needs Docker and an importable `grpc_stubs/` **on
this host** (it aborts if they were cleaned after the build). It starts its own
Redis container — do not run one.

## Never do these

Each turns a port into a rewrite. They are not judgment calls, and the middle
column is the thought that gets you there.

| Move                                | The rationalization                              | Why it is wrong                                                                       |
| ----------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------- |
| Copy a prompt, tool, or schema into the adapter | "so the adapter stands alone"        | It exists in the source. Import it — the whole tree is in the image, and a copy drifts. |
| Swap the LLM provider               | "the image already has one, and the user wants something that runs" | A port that silently changed models does not run *their* project. `requirements:` installs the source's own provider. |
| Hardcode a key, or ship it in a file you add | "there is no other way in"               | The build sweeps the project into every image. Where `env_file:` exists it is the way in; where it does not, say so and stop. |
| Drop or move a dependency           | "this one is obviously dev-only"                 | Obvious to you, not yours to decide. Declare it under `requirements:`; report the rest and let the owner classify. |
| Edit files in the source tree, or vendor it into `agents/` | "just this one line"      | The port leaves `git status` on the source clean, and vendoring is copying.        |
