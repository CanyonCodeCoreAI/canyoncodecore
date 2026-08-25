---
name: porting-to-ventis
description: Use when moving an existing agent project onto Ventis — a LangChain, LangGraph, CrewAI, AutoGen, or hand-rolled pipeline that needs Ventis agent yaml, a global_controller config, and a workflow. Also use when a ported project builds and deploys but the first request answers "No agent loaded".
---
# Porting an agent project to Ventis

## A port is four new files next to an untouched source tree

```
agents/<name>.yaml                 the declaration
agents/<name>_adapter.py           the thinnest class that satisfies Ventis
workflow/<name>_workflow.py        an entry point that calls deploy()
config/global_controller.yaml      the deployment manifest
src/  (or wherever the project lives)   NOT EDITED, NOT COPIED — imported
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
| Copy a prompt, tool, or schema into the adapter         | It exists in the source. Import it.                                                                 |
| Swap the LLM provider (e.g. LangChain/OpenAI → Bedrock) | Changing the model stack is not part of a port. `requirements:` installs the source's own provider. |
| Drop a dependency to make the image build               | Declare it under `requirements:` on the config entry instead.                                       |
| Edit files in the source tree                           | The port must leave `git status` on the source clean.                                               |
| Reimplement a node's body "so it fits in one file"      | The one-file limit is a Ventis defect, not an instruction to inline.                                |

## Step 3 — Walls: stop before you design around one

Two defects in Ventis block real projects. **Check for them before writing the
adapter, not after.**

| Wall                                                                                                 | Check                                                  |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| An agent is exactly one file — only `entrypoint` reaches the build context, no siblings, no packages | Does the adapter import a module from the source tree? |
| A yaml whose basename matches the entrypoint's overwrites the generated stub in the flat context     | Do they share a basename?                              |

Dependencies are not one of them any more. `generate_docker` takes a
`requirements` argument and `cmd_build` passes `_normalize_requirements(agent_cfg)`,
so whatever the source imports beyond the runtime's own list goes on the config
entry — as a list of strings, since anything else is warned about and dropped
whole:

```yaml
requirements:
  - langgraph>=1.0.0
  - langchain-openai>=1.0.0
```

Getting that list wrong is not a wall. It is a `ModuleNotFoundError` in the
agent container's stdout and a first request that answers `"No agent loaded"`.

If the one-file wall applies — and for any LangChain, LangGraph or CrewAI
project it does — **stop and report**:

> This port needs `src/` in the build context: the adapter imports
> `email_assistant`, `prompts` and `schemas`, and `generate_docker` copies only
> the file named by `entrypoint`. Unblocking it needs an `include:` key on the
> agent config entry.

Then stop. Do not continue with a design that avoids the wall.

### Rationalizations that mean you are about to work around a wall

| Thought                                              | Reality                                                                                    |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| "Bedrock is already in the image, I'll use that"     | `requirements:` installs the source's own provider. Swapping the model stack is a rewrite. |
| "I'll inline the prompts so it's one file"           | You are dodging the one-file wall by copying the project. Report it.                       |
| "It's only a few tool functions, copying is simpler" | Copies drift from the source and hide the wall. Report it.                                 |
| "The user wants something that runs"                 | A port that silently changed models does not run *their* project. Report it.               |
| "I'll vendor the source into the agents/ directory"  | Same as copying. Report it.                                                                |

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


When you do split, say plainly what it buys. An agent with `replicas: 1` and no
distinct resource profile is a node Ventis does nothing for.

## Writing the four files

Full contract with sources in `ventis-contract.md`. The parts that bite:

**yaml** — `type` is pasted into an AST unchecked and the stub imports only
`Future` and `inspect`, so use `str` `int` `float` `bool` `dict` `list` and
nothing else. Every declared argument is required (there is no defaults
mechanism). Argument names must equal the Python parameter names character for
character. Give the yaml a basename that differs from the entrypoint's.

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
logs a warning, skips that image, and still exits 0. Everything the source
imports beyond the runtime's own list goes under `requirements:`; that key is the
only way anything gets pip-installed into the image.

## Step 5 — Build, then probe

```bash
ventis build          # requires Docker
```

A green build means the generator ran. It never imports your agent, so it proves
almost nothing — on a real port it prints `Build complete.` and tags every image
for a project whose agent cannot be imported at all.

Ventis then compounds this: the controller writes `healthy` to Redis *before*
loading the agent and a heartbeat keeps re-asserting it, so a container with no
agent stays `healthy` and keeps receiving requests.

So do what the container will do:

```bash
cd docker_container/<AgentName>
python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('m', '<entrypoint basename>.py')
m = importlib.util.module_from_spec(spec); sys.modules['m'] = m
spec.loader.exec_module(m)
m.<AgentName>()
print('ok')
"
```

## Traps, in the order they bite


| Symptom                                         | Cause                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------- |
| `"No agent loaded"` on first request            | anything — the agent container's stdout is the only place the cause exists  |
| A replica reports `healthy` but answers nothing | same; `healthy` is written before the agent loads and never revised         |
| `ModuleNotFoundError` in the agent container    | an import the source needs is missing from the entry's `requirements:`      |
| `NameError` importing a stub                    | a yaml `type` that is not a builtin                                         |
| `TypeError: unexpected keyword argument`        | yaml `arguments[].name` ≠ the Python parameter name                         |
| `.value()` returns a `str` of a dict            | expected — `json.loads` it                                                  |
| Redis holds `<coroutine object ...>`            | the method is `async def`; keep the signature sync and `asyncio.run` inside |
| No faster than the original                     | calls fused with `.value()`; dispatch all, then resolve all                 |
| An agent missing from the deployment            | its config `name` matched no yaml; the build warned and moved on            |
| Debug code runs in production                   | the workflow is `exec`'d, so `__name__ == "__main__"`                       |
| An agent's own stub missing in its container    | yaml basename equals entrypoint basename                                    |
