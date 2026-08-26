---
name: porting-to-ventis
description: Use when porting an existing agent project (LangChain, LangGraph, CrewAI, AutoGen, or a hand-rolled pipeline) onto Ventis — before copying any prompt, tool, or model call into an adapter. Also use when a ported project builds and deploys but its first request answers "No agent loaded".
---

# Porting an agent project to Ventis

## A port is four files beside an untouched source tree

```
agents/<name>.yaml               declares the callable surface
agents/<name>_adapter.py         the thinnest class that satisfies Ventis
workflow/<name>_workflow.py      entry point; calls deploy()
config/global_controller.yaml    deployment manifest
config/policy.yaml               optional — only to restrict access
<the source tree>                NOT EDITED — copied whole into every image
```

Everything the source already does — prompts, tools, schemas, parsing, retries,
its LLM client — is reached with an `import`. **If your port contains a prompt
string, a tool body, or a model call that already exists in the source, you are
rewriting the project, not porting it.**

Mechanism and evidence for every claim here: `ventis-contract.md`.
Symptom-to-cause lookup once something breaks: `traps.md`.

## Step 1 — Survey the source before writing anything

Ventis loads an agent by doing exactly this:

```python
module = <the file named by the config entry's `entrypoint`>
agent  = getattr(module, <yaml's agent.name>)()       # no arguments
result = getattr(agent, <the function name>)(**args)  # synchronous
```

Four questions, all answerable statically — and all expensive to answer after a
green build.

**1. Does the source already expose such a class?** One instantiable with no
arguments, whose methods are plain synchronous functions, needs no adapter —
point `entrypoint` at the file it already lives in and write only the yaml and
the config. Most LangChain and LangGraph projects do not: they expose
module-level functions, `@tool`-decorated objects (`StructuredTool` instances,
not methods), or a compiled graph.

**2. Is the project importable inside the image?** The whole tree is copied in at
its own relative paths, but the container starts at `/app`, so only what landed
flat imports on its own. A `pyproject.toml`, `setup.py` or `setup.cfg` at the
root is what adds `-e .`, and the project's own packaging metadata is what
decides the import root — Ventis never guesses a directory name. No metadata and
the install is skipped, silently. Say so before writing an adapter that imports
across directories; adding packaging metadata to fix it edits the source tree.

**3. When `-e .` runs, what does `[project.dependencies]` drag in?** It resolves
in the same pass as `requirements:`, so whatever the project declares, the image
gets — and workshop projects routinely put their whole toolchain there so that
one install sets a laptop up. Compare each declared name against the source's
imports:

```bash
grep -rl "import <pkg>\|from <pkg>" <source dir>/
```

**Report the mismatch and stop there. Do not move entries, and do not delete
them.** A grep finds names, not requirements: a package loaded from a string at
runtime is imported nowhere and still required. Hand over the list, the cost
(Step 3's protobuf wall, which takes a full image build to reach), and the two
places entries can move to — `[project.optional-dependencies]`, which `-e .`
skips, and `[dependency-groups]`, which never enters package metadata at all.
Then let the owner decide, including deciding not to.

**4. What does the source import beyond the runtime's base list?** Everything
else goes on the config entry's `requirements:`, as a list of strings — anything
else is warned about and dropped whole.

**Credentials are not a wall.** `config/global_controller.yaml` takes an
`env_file:` key — a path, relative to the project root, to a local `.env` that
every container receives as `docker run --env-file`. The file never enters the
image, and `ventis deploy` fails on a bad path before launching anything. A
source that builds its model client at module scope loads fine; declare the keys
it needs and leave the model stack alone.

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

**yaml** — argument `type` is pasted into an AST unchecked, so use `str` `int`
`float` `bool` `dict` `list` and nothing else. Every declared argument is
required. Argument names must equal the Python parameter names character for
character. `returns` is read by nothing — use `type: dict` to mark the call sites
the workflow must `json.loads`.

**adapter** — class name equals `agent.name`; the constructor takes no arguments
(configuration comes from environment variables read in `__init__`); methods are
synchronous, though `asyncio.run(...)` inside the body is fine. Return values
must survive `json.dumps`, and a framework's own state rarely does — convert with
the framework's serializer, never by hand.

**workflow** — a top-level function plus `deploy(fn, port=...)` at the end. The
REST route is the function's `__name__`. The file is `exec`'d rather than
imported, so `__name__ == "__main__"` is true and `if __name__ == "__main__":`
blocks fire in production. `deploy()` blocks.

Dispatch every call before resolving any of them:

```python
futures = [agent.work(item=i) for i in items]        # returns immediately
results = [json.loads(f.value()) for f in futures]   # .value() blocks
```

Fused into one comprehension the calls run one after another. It does not error;
it is just silently serial, and the fan-out is gone.

**config** — each entry's `name` must match a yaml's `agent.name`, or the build
warns, skips that image, and still exits 0. Write `provider: local` in
**lowercase**: the port reservation compares `provider == "local"` with no
normalization, so `Local` leaves the port unreserved and deploy dies.

**policy** — optional. Absent, or present with no rules, every service is
allowed. Write one only to restrict, and then list every service the workflow
reaches; a name left out is not a startup error but an `Unauthorized` response
after the request was accepted.

## Step 3 — Build, then probe the image twice

`ventis build` never imports your agent, so a green build proves almost nothing —
it prints `Build complete.` and tags every image for a project whose container
dies on startup. Ventis compounds this: the controller writes `healthy` to Redis
*before* loading the agent and a heartbeat keeps re-asserting it, so a container
with no agent stays `healthy` and keeps receiving requests.

So run the image — tagged `ventis-<agent.name lowercased>` — and do what the
container does. **Both probes, in this order. Neither covers the other.**

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
| Swap the LLM provider               | "the image already has one, and the user wants something that runs" | A port that silently changed models does not run *their* project. `requirements:` installs the source's own provider, `env_file:` carries its key. |
| Hardcode a key, or ship it in a file you add | "there is no other way in"               | `env_file:` is the way in. Never put a secret in the source tree or the build context.  |
| Drop or move a dependency           | "this one is obviously dev-only"                 | Obvious to you, not yours to decide. Declare it under `requirements:`; report the rest and let the owner classify. |
| Edit files in the source tree, or vendor it into `agents/` | "just this one line"      | The port must leave `git status` on the source clean, and vendoring is copying.        |
