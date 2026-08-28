---
name: porting-to-canyonos-core
description: Use when porting an existing agent project (LangChain, LangGraph, CrewAI, AutoGen, or a hand-rolled pipeline) onto CanyonOS Core
---

# Porting an agent project to CanyonOS Core

CanyonOS Core is the product name. Its current compatibility interface remains
unchanged: the executable is `ventis`, the Python package is `ventis`, runtime
environment variables use `VENTIS_*`, and Docker resources use `ventis-*`.
Treat those as protocol identifiers, not branding strings; do not rename them
while porting.

## How to read the rules in this file

Set in capitals, **MUST** and **NEVER** mark a rule whose violation breaks the
port: the build skips an image, `ventis deploy` dies, or the first request
fails. Every one is indexed in [The MUST list](#the-must-list), whose last
column says whether `ventis build`, deploy preflight, or `validate.py` decides
it. `validate.py` intentionally covers only failures a green image build hides.
Nothing else in this file is written in capitals, so
`grep -nE '\b(MUST|NEVER)\b' SKILL.md` returns the rules and only the rules.

Everything else is stated as fact, in the indicative — how CanyonOS Core behaves, and
what follows from it. There is no "should", and nothing is left to taste that
does not have to be.

## A port is thin scaffolding beside an untouched source tree

```
agents/<name>.yaml               one callable surface per CanyonOS Core service
agents/<name>.py                 one thin adapter per service, when needed
workflow/<name>_workflow.py      entry point; calls deploy()
config/global_controller.yaml    deployment manifest
config/policy.yaml               optional — only to restrict access
pyproject.toml                   conditional — only to expose a nested import root
<the source tree>                NOT EDITED — copied whole into every image
```

The file count follows the deployment design. A single adapted service normally
adds one yaml/adapter pair, one workflow, and one config. A multi-agent port adds
one yaml/adapter pair for each service that Rule 2 justifies splitting out. If a
source class already satisfies the CanyonOS Core contract, its config can point to that
source file directly and no adapter copy is needed.

The project root is the directory from which `ventis build` runs. The source
remains untouched below it. A root `pyproject.toml` is additional conditional
scaffolding when the source is nested, its original imports do not resolve from
`/app`, and the target CanyonOS Core supports an editable install. Metadata inside the
nested source tree does not trigger that install.

The two `agents/` files share one basename, as every example does. The build
generates a stub from the yaml and copies it to `agents/<basename>.py` in every
image; in the agent's own image the adapter is copied afterwards and wins the
flat name back, so the two never collide. Pick a basename that is not a module
the adapter imports — an adapter beside a source package called `memory_agent`
is named something else, or it shadows the package it exists to import.

Everything the source already does — prompts, tools, schemas, parsing, retries,
its LLM client — is reached with an `import`. **A port that contains a prompt
string, a tool body, or a model call that already exists in the source is a
rewrite of the project, not a port of it.**

Mechanism and evidence for every claim here: `canyonos-core-contract.md`.
Symptom-to-cause lookup once something breaks: `traps.md`.

## Step 1 — Survey the source before writing anything

CanyonOS Core loads an agent by doing exactly this:

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

- **The import root** — run `validate.py` first and read its
  `editable_install` capability. When available, a `pyproject.toml`, `setup.py`
  or `setup.cfg` at the **port root** adds `-e .`; metadata inside the nested
  source does not. Add a minimal root `pyproject.toml` only when an original
  import cannot resolve from `/app`. It names the existing source directory and
  package, declares no dependencies, and does not reference a README or license:

  ```toml
  [build-system]
  requires = ["setuptools>=64"]
  build-backend = "setuptools.build_meta"

  [project]
  name = "ventis-port"
  version = "0.0.0"
  dependencies = []

  [tool.setuptools.packages.find]
  where = ["<directory containing the source package>"]
  include = ["<source package>*"]
  namespaces = true
  ```

  Set `where` from the actual tree; for a wrapped project with
  `source/pyproject.toml` and `source/src/pkg/`, it is `source/src`, not
  `source`. Keep dependencies where the source declared them. Because nested
  metadata is not installed, repeat its runtime distributions under each
  config entry's `requirements:` without editing or deleting the source list.
  Without editable-install support, report an import that cannot resolve from
  `/app` and stop. A directory rooted directly at `/app` can already resolve as
  a Python namespace package even without `__init__.py`; do not add packaging
  metadata merely because that file is absent.

- **`env_file:`** — *needs PR #53, open against main.* A path relative to the
  project root pointing at a local `.env`, handed to every container as
  `docker run --env-file`. The file never enters the image, and `ventis deploy`
  fails on a bad path before launching anything. Without that PR, the only
  variables reaching a container are five `VENTIS_*` names, and a config that
  sets `env_file:` is setting a key nothing reads — the credential is silently
  dropped and the failure surfaces as a provider error on the first request.

### When the target includes `llm_proxy`

The proxy is an endpoint redirect, not a provider conversion. Keep the source's
OpenAI, Anthropic, or boto3 client and its request format; put the corresponding
SDK variable in the runtime env file:

```dotenv
OPENAI_BASE_URL=http://host.docker.internal:8081/openai/v1
ANTHROPIC_BASE_URL=http://host.docker.internal:8081/anthropic
AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://host.docker.internal:8081/bedrock
```

Use only the lines for each provider the source actually uses. Its caller-side
credentials are placeholders, for example:

```dotenv
OPENAI_API_KEY=proxy-placeholder
ANTHROPIC_API_KEY=proxy-placeholder
AWS_ACCESS_KEY_ID=proxy-placeholder
AWS_SECRET_ACCESS_KEY=proxy-placeholder
AWS_REGION=us-east-1
```

OpenAI and Anthropic SDKs still require an API-key variable and boto3 still
requires credentials with which to sign the request, even though the proxy
replaces or reissues those credentials upstream. Launch the proxy in a separate
environment holding the real credentials. Do not put real proxy credentials in
the port's `env_file`, which is given to every agent and workflow container.

The current proxy is local and non-streaming. Start it on the Docker host with a
non-loopback bind and a port different from the workflow API's usual 8080:

```bash
PROXY_HOST=0.0.0.0 PROXY_PORT=8081 python -m llm_proxy
curl http://127.0.0.1:8081/healthz
```

Local CanyonOS Core containers can resolve `host.docker.internal` because their
`docker run` includes `--add-host=host.docker.internal:host-gateway`. An EC2
container resolves that name to its own EC2 Docker host, not the machine running
`ventis deploy`; a local-only proxy therefore does not support a distributed
port. A reachable proxy address or one proxy per host is deployment work to
report, not an adapter rewrite.

Survey the source for streaming before choosing this route: OpenAI
`stream=True`, Anthropic stream APIs, Bedrock `invoke_model_with_response_stream`,
Converse, and Converse Stream are outside this implementation. Do not silently
turn streaming off. Report the unsupported call and stop. Exact mechanics and
failure signatures are in `canyonos-core-contract.md` and `traps.md`.

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
framework runtime.** CanyonOS Core has no runtime to execute a LangGraph `StateGraph`, a
CrewAI `Crew` or an AutoGen `GroupChat`, so their wiring is re-expressed as
ordinary Python — in the workflow when it fans out, in the adapter when it does
not. The nodes those edges connected are imported, unchanged.

| Source code                                                | Treatment                      |
| ---------------------------------------------------------- | ------------------------------ |
| `StateGraph` / `add_edge` / `Send` / `Command(goto=...)`   | rewrite as Python control flow |
| `Crew(...)` / `GroupChat(...)` assembly                    | rewrite as Python control flow |
| node functions, prompts, tools, schemas, parsers, clients  | **import**                     |
| the source's model provider and SDK                        | **keep**                       |
| a runtime object the nodes read services off                | **construct one** — see below  |

**A framework runtime supplies two things, and only one of them is edges.** It
also injects services the nodes read at call time: LangGraph hands each node a
`Runtime` and the node reads `runtime.store`, `runtime.context`; other
frameworks pass a memory, a callback manager, a session. CanyonOS Core injects none of
it, so the adapter builds the object and passes it in — that is part of
re-expressing the runtime, not a liberty taken with the source.

**Configure it from what the project already declares, never from taste.** A
LangGraph project states its store in `langgraph.json`; copy those values rather
than choosing your own, because an invented embedding model or dimension is a
silent change to what the project does. Where the project declares nothing, say
in the port report what you chose and why.

## Rule 2 — Split only to scale

**Splitting into multiple agents is a scaling decision, not a format
requirement.** A single agent holding the whole pipeline is a valid CanyonOS Core
project. Start there, and hoist a loop into the workflow only when each iteration
fans out to more than one node:

- a single-agent ReAct loop **stays whole in one agent** — every turn needs the
  full message history, and hoisting pushes a growing message list through Redis
  each turn.
- a supervisor handing out N tasks, or a `Send` fan-out, is **hoisted** — N
  independent runs per request with no shared state is what replicas pay for.

An agent with `replicas: 1` and no distinct resource profile is a node CanyonOS Core
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

Its two imports are fixed, and neither is guessable:

```python
from deploy import deploy                          # flat: deploy.py is copied to /app
from agents.<basename> import <AgentName>          # the stub, under agents/
```

**The stub is only at `agents/`, and its class carries the agent's own name.**
Two traps sit here, and the build walks you into both:

- `ventis build` prints `Generated stub class '<AgentName>Stub'`, but the class
  it writes is `<AgentName>`. The message is computed separately from the code.
  Importing what it names raises `ImportError`.
- The flat form `from <basename> import <AgentName>` is what the examples in
  this repository use, and in the workflow image it raises
  `ModuleNotFoundError`: the stub is copied to one path, and for the workflow
  that path is `agents/<basename>.py`. No `__init__.py` is needed — `agents/`
  resolves as a namespace package.

CanyonOS Core itself is permissive here: it serves `POST /<fn.__name__>` and splats the
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
| M6  | Every config entry `name` MUST match some yaml `agent.name`                | build      |
| M7  | Two config entry `name`s MUST differ by more than case                     | build output |
| M8  | `provider` MUST be lowercase `local` (EC2 takes any casing)                | deploy preflight |
| M9  | `replicas` MUST be an integer                                              | deploy preflight |
| M10 | `requirements:` MUST be a list of strings                                  | build      |
| M11 | The workflow MUST expose `main(query)`; other parameters MUST have defaults | build + V016 |
| M12 | The workflow MUST NEVER carry an `if __name__ == "__main__":` block        | V017       |
| M13 | A fan-out MUST dispatch every call before resolving any                    | V018       |
| M14 | No project module MUST take the flat name of a runtime file or a stub      | V019, V020 |
| M14b | The workflow MUST import a stub as `from agents.<basename> import <AgentName>` | V023 |
| M15 | `policy.yaml` MUST be absent, or MUST carry a non-empty `rules:` list      | deploy preflight |
| M16 | An EC2 entry MUST declare `instance_type`, and `ec2:` MUST be complete     | deploy preflight |
| M17 | NEVER copy a prompt, tool, or schema that exists in the source             | review     |
| M18 | NEVER hardcode a credential, or ship one in the build context              | W003       |
| M19 | NEVER edit the source tree, and NEVER vendor it into `agents/`             | `git status` |
| M20 | NEVER swap the LLM provider the source uses; an LLM proxy only redirects its endpoint | -- |
| M21 | NEVER move or drop a declared dependency — report it and stop              | --         |
| M22 | Framework control flow MUST be rewritten; everything else MUST be imported | --         |

Build owns YAML parsing, required paths, stub generation, and Dockerfile/package
installation errors. Deploy preflight owns provider, replica, policy, and EC2
shape. `validate.py` does not repeat those checks; it focuses on adapter loading,
stub imports, workflow execution, copy collisions, credentials, import roots,
and dependencies that fail only inside a built container.

Two more rules apply only where the CanyonOS Core you are targeting supports them,
which `validate.py` probes for rather than assumes:

| #   | The rule                                                          | Needs                   | Check |
| --- | ----------------------------------------------------------------- | ----------------------- | ----- |
| M23 | `env_file:` MUST resolve to a readable file, and MUST be the only way a credential enters | PR #53 | deploy preflight; support V030 |
| M24 | A source import that does not resolve from `/app` MUST have usable packaging metadata at the port root | editable install | V031 |

## Step 3 — Preflight hidden runtime failures

```bash
python <skill_dir>/validate.py .
```

Run it before building. It does not duplicate errors `ventis build` or deploy
preflight already reports. Instead it catches what those stages do not execute:
the adapter class contract, generated-stub import path, workflow behavior, flat
copy collisions, container credentials, package import roots, and undeclared
runtime imports.

It parses Python but never imports the port, so it is safe on a tree whose
dependencies are not installed. Errors are deterministic runtime contract
violations and exit 1. Heuristic warnings exit 0 unless `--strict` is used.
`--json` emits the findings as data. A malformed config or agent yaml is reported
by `ventis build`; when it prevents runtime inspection, the validator emits only
a `BUILD` informational finding and stops.

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

# 2. The agent, loaded the way _load_agent loads it. --env-file because the
#    constructor reads the environment, and the deployment gives it one.
docker run --rm --env-file <the env_file path> ventis-<name> python -c "
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

It takes `--env-file` because `__init__` reads the environment and a container
started by `ventis deploy` gets one. Without it a correct port fails its own
probe on a missing credential — an adapter that builds an embeddings client in
its constructor raises `OpenAIError: Missing credentials` and passes unchanged
the moment the file is passed.

Then `ventis deploy`, which needs Docker and an importable `grpc_stubs/` **on
this host** (it aborts if they were cleaned after the build). It starts its own
Redis container — do not run one.

## Step 5 — Clean up every build and deployment product

Do this after recording the probe and request results, including on failure
paths. First stop the foreground `ventis deploy` with Ctrl+C and wait for
`GlobalController cleanup` to remove its agent, workflow, and Redis containers.
If deploy crashed before its cleanup handler ran, remove the exact container
names created by this deployment; do not delete another project's containers.

Then remove generated files and the exact images built from the config:

```bash
ventis clean    # removes stubs/, grpc_stubs/, and docker_container/

docker image rm \
  ventis-<agent-name-lowercased> \
  ventis-<workflow-entry-name-lowercased>
```

Repeat the image argument for every config entry. `ventis clean` does not remove
containers or images. Confirm that the project root no longer contains the three
generated directories and that no container from this deployment remains:

```bash
test ! -e stubs && test ! -e grpc_stubs && test ! -e docker_container
docker ps -a --format '{{.Names}}'
```

Keep all agent declarations and adapters, the workflow, config, conditional
root `pyproject.toml`, untouched source tree, and any requested logs or port
report. Those are source and evidence, not build products.

## Never do these

Each turns a port into a rewrite. They are not judgment calls, and the middle
column is the thought that gets you there.

| Move                                | The rationalization                              | Why it is wrong                                                                       |
| ----------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------- |
| Copy a prompt, tool, or schema into the adapter | "so the adapter stands alone"        | It exists in the source. Import it — the whole tree is in the image, and a copy drifts. |
| Swap the LLM provider               | "the image already has one, and the user wants something that runs" | A port that silently changed models does not run *their* project. `requirements:` installs the source's own provider; `llm_proxy` redirects that SDK rather than converting its request. |
| Hardcode a key, or ship it in a file you add | "there is no other way in"               | The build sweeps the project into every image. Where `env_file:` exists it is the way in; with `llm_proxy`, it contains routing plus dummy caller credentials while the proxy receives real credentials separately. |
| Drop or move a dependency           | "this one is obviously dev-only"                 | Obvious to you, not yours to decide. Declare it under `requirements:`; report the rest and let the owner classify. |
| Edit files in the source tree, or vendor it into `agents/` | "just this one line"      | The port leaves `git status` on the source clean, and vendoring is copying.        |
