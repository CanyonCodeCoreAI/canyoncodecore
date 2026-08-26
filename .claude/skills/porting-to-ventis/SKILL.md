---
name: porting-to-ventis
description: Use when moving an existing agent project onto Ventis — a LangChain, LangGraph, CrewAI, AutoGen, or hand-rolled pipeline that needs Ventis agent yaml, a global_controller config, and a workflow. Also use when a ported project builds and deploys but the first request answers "No agent loaded".
---
# Porting an agent project to Ventis

## A port is five new files next to an untouched source tree

```
agents/<name>.yaml                 the declaration
agents/<name>_adapter.py           the thinnest class that satisfies Ventis
workflow/<name>_workflow.py        an entry point that calls deploy()
config/global_controller.yaml      the deployment manifest
config/policy.yaml                 required — deploy crashes without it
src/  (or wherever the project lives)   NOT EDITED — copied whole into the image
```

That is the whole output. Everything the source project already does — prompts,
tools, schemas, parsing, retries, its LLM client — is reached with an `import`.
If your port contains a prompt string, a tool body, or a model call that already
exists in the source tree, you are rewriting the project, not porting it.

## Step 1 — Does the source already satisfy Ventis?

Ventis loads an agent by doing exactly this:

```python
module = <import the file named by config's `entrypoint`>
agent  = getattr(module, <yaml's agent.name>)()   # no arguments
result = getattr(agent, <yaml's function name>)(**args)   # synchronous
```

So it needs *a class, instantiable with no arguments, whose methods are plain
synchronous functions*. `entrypoint` may point at a file that already exists in
the source project — if the project has such a class, write only the yaml and
the config and stop. No adapter.

Most LangChain and LangGraph projects do not. They expose module-level
functions, `@tool`-decorated objects (which are `StructuredTool` instances, not
methods), or a compiled graph:

```python
email_assistant = overall_workflow.compile()
```

Those need an adapter, and the adapter is small:

```python
# agents/email_agent.py
from langchain_core.messages import messages_to_dict  # the framework's own serializer
from email_assistant import email_assistant           # the source, untouched

class EmailAssistant(object):
    def run(self, email_input: dict) -> dict:
        result = email_assistant.invoke({"email_input": email_input})
        return {
            "classification_decision": result.get("classification_decision"),
            "messages": messages_to_dict(result.get("messages", [])),
        }
```

`asyncio.run(...)` inside the method is fine when the source is async — Ventis
constrains the method *signature*, not its body.

Passing the initial state positionally is deliberate: it keeps the framework's
own parameter name (`invoke(input=...)`) out of the yaml, which otherwise has to
declare an argument called `input` and breaks when the framework renames it.

**Serializing the result is the adapter's other half.** `_execute_locally` does
`json.dumps()` on a dict result, and a framework's own state is rarely
JSON-safe — a LangGraph `MessagesState` holds `AIMessage` objects, which
`json.dumps` refuses. Convert with the framework's serializer, never by hand:
`m.content` is an empty string on a tool call, so a hand-rolled comprehension
silently drops every tool the agent used. Skip this and the build is still
green, the replica still `healthy`, and `.value()` raises `Object of type
AIMessage is not JSON serializable` on the first request.

## Step 2 — Rewrite orchestration only

One kind of source code genuinely cannot be reused: **control flow owned by a
framework runtime.** A LangGraph `StateGraph`, a CrewAI `Crew`, an AutoGen
`GroupChat` — Ventis has no runtime to execute these, so their orchestration has
to be re-expressed as ordinary Python.

Rewrite the *orchestration function*. Import everything it orchestrates.

```python
# The graph is gone; the nodes it called are not.
from email_assistant import triage_router, llm_call, tool_node, should_continue
from prompts import triage_system_prompt
from tools import get_tools_by_name

class EmailAssistant(object):
    def run(self, email_input: dict) -> dict:
        state = {"email_input": email_input, "messages": []}
        decision = triage_router(state)        # the source's own node function
        ...
```

The test for whether something may be rewritten:

| Source code                                               | Treatment                      |
| --------------------------------------------------------- | ------------------------------ |
| `StateGraph` / `add_edge` / `Command(goto=...)` wiring    | rewrite as Python control flow |
| CrewAI `Crew(...)` / AutoGen `GroupChat(...)` assembly    | rewrite as Python control flow |
| node functions, prompts, tools, schemas, parsers, clients | **import**                     |
| the source's model provider and SDK                       | **keep**                       |

## Never do these

Each of these turns a port into a rewrite. They are not judgment calls.

| Move                                                    | Why it is wrong                                                                                     |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Copy a prompt, tool, or schema into the adapter         | It exists in the source. Import it — the whole tree is in the image.                                |
| Swap the LLM provider (e.g. LangChain/OpenAI → Bedrock) | Changing the model stack is not part of a port. `requirements:` installs the source's own provider. |
| Drop a dependency to make the image build               | Declare it under `requirements:` on the config entry instead.                                       |
| Edit files in the source tree                           | The port must leave `git status` on the source clean.                                               |
| Inline a module "so the adapter is self-contained"      | Nothing limits an agent to one file any more. There is no reason left to inline.                    |

## Step 3 — Three checks before you write the adapter

The build context used to be the wall: an agent was exactly one file, so an
adapter could not import the source tree it wrapped. That is gone.
`generate_docker` takes a `project_dir` and copies the whole project into every
image with its relative paths intact, so **an adapter may import anything in the
project**, and the yaml basename no longer matters.

What is left is cheaper to check now than to debug after a green build.

### Check 1 — is the project installable?

Copying the tree in is not the same as putting it on `sys.path`. The container
runs `python local_controller.py` from `/app`, so `sys.path[0]` is `/app` and
only what landed flat there imports.

```
Is there a pyproject.toml, setup.py or setup.cfg at the project root?
├── Yes → the Dockerfile adds `-e .`, and the source's own packaging metadata
│         decides the import root. `[tool.setuptools.package-dir] "" = "src"`
│         is what makes `from prompts import ...` resolve inside src/.
│         Nothing to do.
└── No  → the install is skipped, silently — no warning anywhere. The tree is
          still copied, but only modules that landed flat import. Say so before
          writing an adapter that imports across directories; adding packaging
          metadata to fix it edits the source tree.
```

The import root always comes from the source, never from a guess — a project
that calls its root `lib/` or `app/` works for the same reason `src/` does, and
Ventis never has to know the name.

### Check 2 — does the source need a credential to import?

**This is the wall.** `_launch_locally` builds its `docker run` with five `-e`
flags, all `VENTIS_*`, and `.env` is excluded from the build context. There is
no mechanism of any kind for passing a secret to an agent container.

Grep the source for a client constructed at module scope:

```python
model = ChatOpenAI(model="gpt-4o")     # module scope -> runs at import
llm = init_chat_model("openai:...")    # same
```

```
Found one?
├── Yes → **stop and report.** The container cannot load the agent at all.
└── No (built inside a function, or lazily) → continue.
```

Observed on the email_assistant image with no key in the environment:
`_load_agent` logs `Missing credentials ... set the OPENAI_API_KEY`, returns
`None`, and the replica still reports `healthy`. Unblocking it needs an `env:`
key on the config entry, or pass-through of named host variables. Neither
exists.

### Check 3 — what does the source import beyond the runtime's list?

Not a wall. `generate_docker` takes a `requirements` argument and `cmd_build`
passes `_normalize_requirements(agent_cfg)`, so anything the source needs goes
on the config entry — as a list of strings, since anything else is warned about
and dropped whole:

```yaml
requirements:
  - langgraph>=1.0.0
  - langchain-openai>=1.0.0
```

When Check 1 said yes, `-e .` already installs whatever the project's own
metadata declares, and this key covers only what sits outside it. Getting the
list wrong is a `ModuleNotFoundError` in the agent container's stdout and a
first request that answers `"No agent loaded"`.

### Rationalizations that mean you are about to work around a wall

| Thought                                              | Reality                                                                                    |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| "Bedrock is already in the image, I'll use that"     | `requirements:` installs the source's own provider. Swapping the model stack is a rewrite. |
| "I'll inline the prompts so the adapter stands alone" | The whole tree is in the image. Inlining only creates a copy that drifts.                  |
| "I'll hardcode the key / read it from a file I add"  | The credential wall is real. Report it; do not ship a secret in a build context.           |
| "The user wants something that runs"                 | A port that silently changed models does not run *their* project. Report it.               |
| "I'll vendor the source into the agents/ directory"  | Same as copying, and pointless now. Import it.                                             |

## Step 4 — Decide the split, separately

**Splitting into multiple agents is a scaling decision, not a format
requirement.** A single agent holding the whole pipeline is a valid Ventis
project. Start there.

Split only when a node needs its own scaling or its own resource profile — and
the test is: **does each iteration of this loop fan out to more than one node?**

| Loop                                                         | Placement                                                                                                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| single-agent ReAct (`AgentExecutor`, a researcher subgraph)  | stays whole in one agent — each turn needs the full message history, and hoisting it pushes a growing message list through Redis every turn |
| cross-agent orchestration (a supervisor handing out N tasks) | hoisted into the workflow — each turn fans out across replicas, which is the only reason to be on Ventis                                    |
| a LangGraph `Send` fan-out (`examples/map_reduce`)           | hoisted — N independent runs per request with no shared state is exactly the shape replicas pay for                                         |

When you do split, say plainly what it buys. An agent with `replicas: 1` and no
distinct resource profile is a node Ventis does nothing for.

## Writing the five files

Full contract with sources in `ventis-contract.md`. The parts that bite:

**yaml** — `type` is pasted into an AST unchecked and the stub imports only
`Future` and `inspect`, so use `str` `int` `float` `bool` `dict` `list` and
nothing else. Every declared argument is required (there is no defaults
mechanism). Argument names must equal the Python parameter names character for
character.

Mark every `returns.type: dict` — that is the list of call sites the workflow
must `json.loads`.

**adapter** — class name equals `agent.name`; no constructor arguments
(configuration comes from environment variables read in `__init__`); methods are
synchronous.

**workflow** — a top-level function plus `deploy(fn, port=...)` at the end. The
REST route is the function's `__name__`, not a fixed `/main`. `deploy()` blocks,
so nothing after it runs. The file is `exec`'d, not imported, so
`__name__ == "__main__"` is true and `if __name__ == "__main__":` blocks fire in
production.

When the workflow drives more than one agent, dispatch every call before
resolving any of them:

```python
futures = [agent.work(item=i) for i in items]   # returns immediately
results = [json.loads(f.value()) for f in futures]   # .value() blocks
```

Fused into one comprehension the calls run one after another. It does not error;
it is just silently serial, and the fan-out is gone.

**config** — the entry's `name` must match a yaml's `agent.name`, or the build
logs a warning, skips that image, and still exits 0.

Write `provider: local` in **lowercase**. `InstanceManager` compares
`provider == "local"` with no normalization, so `Local` skips port reservation,
leaves `reserved_port` as `None`, and `ventis deploy` fails with
`int() argument must be ... not 'NoneType'`. The EC2 check on the same value is
`.upper() == "EC2"`, so only this one is case-sensitive.

**policy** — `config/policy.yaml` is not optional, whatever the name suggests.
`_load_policy_rules` bare-`return`s `None` when the file is absent and
`_load_and_write_policies` immediately calls `len()` on it, so `ventis deploy`
dies with `TypeError: object of type 'NoneType' has no len()` before a single
container starts. Every example that ships one hides this, so **write it, always**:

```yaml
rules:
  - match: {}          # no match keys -> the default rule
    access:
      - EmailAssistant   # every agent the workflow reaches
      - Workflow
```

List every service by name. A missing one is not a startup error — it is
`Unauthorized: Policy denied access to service 'X'` in the `/status` response,
after the request was accepted, which is exactly what `examples/finance` returns
because its default rule omits `VllmAgent`. Write `access: all` only when you
mean every service.

## Step 5 — Build, then probe the image twice

```bash
ventis build          # requires Docker
```

A green build means the generator ran. It never imports your agent, so it proves
almost nothing — on a real port it prints `Build complete.` and tags every image
for a project whose container dies on startup.

Ventis then compounds this: the controller writes `healthy` to Redis *before*
loading the agent and a heartbeat keeps re-asserting it, so a container with no
agent stays `healthy` and keeps receiving requests.

So run the image and do what the container does. The image is tagged
`ventis-<agent.name lowercased>`. **Both probes, in this order.**

```bash
# 1. The runtime itself. This is what CMD runs, and it fails before your agent
#    is ever reached, so probing the entrypoint alone will miss it.
docker run --rm ventis-<agentname> python -c "import local_controller"

# 2. The agent, loaded the way _load_agent loads it.
docker run --rm ventis-<agentname> python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('m', '<entrypoint basename>.py')
m = importlib.util.module_from_spec(spec); sys.modules['m'] = m
spec.loader.exec_module(m)
m.<AgentName>()
print('ok')
"
```

Probe 1 exists because of the second wall. `cmd_build` runs `grpc_tools.protoc`
on the **host** and copies the generated `_pb2.py` into the image, where a
resolver that knows nothing about them picks the protobuf runtime. Protobuf
refuses to load gencode newer than its runtime, so a source whose dependencies
hold protobuf back kills the container on `import local_controller`:

```
google.protobuf.runtime_version.VersionError: Detected incompatible Protobuf
Gencode/Runtime versions when loading local_controler.proto:
gencode 7.35.1 runtime 6.33.6.
```

Nothing pins this. An image with few requirements resolves to the newest wheel
and passes by coincidence, which is why it looks like it works until a real
dependency tree lands. Report it; the fix belongs in `generate_docker`, not in
the port.

Then `ventis deploy`, which needs three things beyond a green build: Docker, an
importable `grpc_stubs/` **on this host** (it aborts with `generated grpc_stubs
are missing or not importable` if they were cleaned after the build), and
`config/policy.yaml`. It starts its own Redis container — do not run one.

## Traps, in the order they bite

| Symptom                                          | Cause                                                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| `TypeError: object of type 'NoneType' has no len()` on deploy | no `config/policy.yaml`; the loader returns `None` and the writer calls `len()` on it |
| `int() argument must be ... not 'NoneType'` on deploy | `provider:` is not lowercase `local`, so no host port was reserved |
| `generated grpc_stubs are missing or not importable` | `ventis build` has not run on this host, or its output was cleaned                     |
| `Unauthorized: Policy denied access to service 'X'` | `X` is missing from the `access` list of the rule that matched                         |
| Container exits on `import local_controller`     | protobuf gencode newer than the resolved runtime; nothing pins the gRPC stack             |
| `"No agent loaded"` on first request             | anything — the agent container's stdout is the only place the cause exists                |
| A replica reports `healthy` but answers nothing  | same; `healthy` is written before the agent loads and never revised                       |
| `Missing credentials` loading the agent          | the source builds its model client at import; nothing can pass a secret into the container |
| `ModuleNotFoundError` for the source's own modules | the project declares no packaging metadata, so `-e .` was skipped and only flat modules import |
| `ModuleNotFoundError` for a third-party package  | an import the source needs is missing from the entry's `requirements:`                    |
| `NameError` importing a stub                     | a yaml `type` that is not a builtin                                                       |
| `TypeError: unexpected keyword argument`         | yaml `arguments[].name` ≠ the Python parameter name                                       |
| `.value()` returns a `str` of a dict             | expected — `json.loads` it                                                                |
| `Object of type AIMessage is not JSON serializable` | the adapter returned framework objects; serialize with the framework's own serializer  |
| Redis holds `<coroutine object ...>`             | the method is `async def`; keep the signature sync and `asyncio.run` inside               |
| No faster than the original                      | calls fused with `.value()`; dispatch all, then resolve all                               |
| An agent missing from the deployment             | its config `name` matched no yaml; the build warned and moved on                          |
| Debug code runs in production                    | the workflow is `exec`'d, so `__name__ == "__main__"`                                     |
