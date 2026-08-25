---
name: porting-to-ventis
description: Use when moving an existing agent project onto Ventis — a LangChain, LangGraph, CrewAI, AutoGen, or hand-rolled pipeline that needs to become Ventis agents, agent yaml, a global_controller config, and a workflow. Also use when a ported project builds and deploys but the first request answers "No agent loaded".
---

# Porting an agent project to Ventis

## The one thing to know

**Ventis accepts a broken project without complaining.** `_load_agent` swallows
every exception. A missing dependency, a sibling module that never reached the
build context, a class name one letter off — all three produce a green `ventis
build`, a green `ventis deploy`, and then `"No agent loaded"` on the first
request, with the real cause buried in a container's stdout.

It is worse than a silent failure: the controller writes `healthy` to Redis
*before* it tries to load the agent, and a heartbeat keeps re-asserting it. A
container with no agent at all stays `healthy` forever and keeps receiving
requests. Nothing upstream can tell the difference.

So the work is not "write four kinds of file." It is: make each decision
checkable at the moment you make it, because the toolchain will not check it for
you.

Read `ventis-contract.md` in this directory before writing anything. Verify its
claims against `ventis/` — line numbers drift, and `examples/` is unreliable
(committed merge-conflict markers in `examples/portfolio/`, a stale
`ExampleAgentStub` import in `examples/helloworld/`).

## Step 1 — Find where control flow lives

Ask one question: **does a framework runtime execute the pipeline, or does
Python?**

| Source | Control flow | Rewrite needed |
|---|---|---|
| LangGraph | `StateGraph`, `Command(goto=...)`, state reducers | yes — the graph and the automatic state merge both come apart |
| AutoGen | `GroupChat` speaker selection | yes — an LLM picks the next speaker |
| CrewAI `hierarchical` | a manager LLM assigning tasks | yes |
| CrewAI `sequential` | `Crew(tasks=[...])` | light — the order is already declared |
| LangChain `AgentExecutor` | its internal think→act→observe loop | usually none, see Step 2 |
| LCEL (`a \| b \| c`) | `Runnable.__or__` | light — a pipe is already linear |
| hand-rolled Python | your own code | none |

Write down the actual execution order before touching anything. In LangGraph it
is not written in any single place — you reconstruct it from every node's
`Command(goto=...)` plus the `add_edge` calls.

## Step 2 — Decide the split

This is the only step that needs human sign-off, and it is expressed as
`agents/*.yaml`. One yaml per node.

**The test for hoisting a loop into the workflow: does each iteration fan out to
more than one node?**

| Loop | Where it goes | Why |
|---|---|---|
| single-agent ReAct — `AgentExecutor`, a researcher subgraph | stays whole, inside one agent | each turn needs the full message history; hoisting it means shuttling `list[ToolMessage]` across Redis every turn, for nothing |
| cross-agent orchestration — a supervisor handing out N tasks | hoisted into the workflow | each turn fans out across replicas, which is the entire reason to be on Ventis |

The unit of splitting is **an independent resource profile or an independent
scaling need** — not "the source project called this a Tool." A node that is
called once per request, does no I/O the others don't, and never needs a second
replica belongs merged into its caller. Splitting it buys a Redis round-trip and
a serialization boundary and nothing else.

Two consequences worth stating out loud when you present the split:

- Any agent you give `replicas: 1` and no distinct resource profile is a node
  Ventis does nothing for.
- Some nodes must stay together because what passes between them cannot be
  flattened to JSON. That is a legitimate reason to merge, and it is discovered
  here, not later.

Present the drafted yaml with the reasoning behind each boundary, and let the
user overrule it before you write any Python.

### Offer the incremental path

For a large source project, offer this as step 0 rather than a full rewrite:
wrap the entire existing pipeline as **one** agent with **one** method; the
workflow calls it once.

```python
class DeepResearchAgent(object):
    def research(self, query: str) -> dict:
        return asyncio.run(existing_pipeline.ainvoke({"messages": [...]}))
```

`asyncio.run` inside a synchronous method is fine — Ventis constrains the method
*signature*, not the body. This buys nothing from Ventis (no fan-out, no
scheduling), and say so plainly. What it buys is a deployment that works, after
which the one high-fan-out node can be split out on its own. Without this
option, the user's only choice is rewriting everything at once.

## Step 3 — Write the files

Each recipe below produces something checkable.

### `agents/<x>.yaml`

Types are pasted into an AST unchecked and the stub imports only `Future` and
`inspect`. Use `str` `int` `float` `bool` `dict` `list` and nothing else — a
`List[str]` gives you `NameError` at import.

Mark every `returns.type: dict`. That mark is the list of call sites the
workflow must `json.loads`.

**Give the yaml a basename that differs from the entrypoint's.** The generated
stub is named after the yaml (`stubs/<yaml basename>.py`), and the Docker
context is flat, so `agents/x.yaml` + `agents/x.py` put two different files at
`/app/x.py`. The implementation is copied last and wins — the agent loses its
own stub with no warning. `agents/research.yaml` alongside
`agents/research_agent.py` avoids it.

### `agents/<x>.py`

The class:

- name equals `agent.name`, exactly
- takes no constructor arguments — configuration comes from environment
  variables read in `__init__`
- exposes plain synchronous methods whose parameter names equal the yaml's
  `arguments[].name`, character for character
- returns `dict`, `list`, or something `str()` renders usefully

`@tool`-decorated functions do not survive: the decorator produces a
`StructuredTool` object, and `getattr(agent, "fn")` will not find a callable.
Unwrap them into methods.

Anything the source project did with framework config injection
(`RunnableConfig`, CrewAI constructor kwargs) becomes environment variables,
because there is no injection point.

### `workflow/<name>.py`

The shape:

```python
def main(query: str = ""):
    supervisor = SupervisorAgent()
    researcher = ResearcherAgent()

    brief = json.loads(supervisor.write_brief(query=query).value())["research_brief"]

    notes = []
    for _ in range(MAX_ITERATIONS):                 # was: supervisor <-> supervisor_tools
        plan = json.loads(supervisor.plan(brief=brief).value())
        if plan["done"]:                            # was: Command(goto=END)
            break

        futures = [researcher.research(topic=t) for t in plan["topics"]]  # dispatch all
        results = [json.loads(f.value()) for f in futures]                # then resolve all

        notes.extend(r["compressed_research"] for r in results)  # was: a state reducer

    return {"notes": notes}

deploy(main, port=8080)
```

Four things that shape is carrying:

**Dispatch every call before resolving any of them.** `research()` returns a
Future immediately; `.value()` blocks. Fused into one comprehension —
`[f(x).value() for x in xs]` — the calls run one after another. It does not
error. It is just silently serial, and the fan-out that justified the port is
gone.

**Agents return decisions; the workflow branches.** An agent in its own
container cannot `goto` another agent. Anything that was dynamic routing becomes
a method that returns a choice, plus an `if` in the workflow.

**State merges are yours now.** Whatever a reducer accumulated
(`Annotated[list, operator.add]`) becomes an explicit `.extend()` here.

**Everything crossing a call boundary is JSON.** `.value()` hands back a string,
always. Pydantic models get `.model_dump()`, framework message objects get
flattened, and anything that resists flattening is a signal that those two nodes
should not have been split (back to Step 2).

Module-level code in this file runs once at container start; `main()` runs per
request. Expensive setup belongs in an agent's `__init__` — agent containers are
long-lived.

### `config/global_controller.yaml`

One entry per agent (`name`, `entrypoint`, `replicas`, `resources`, `provider`),
plus one `type: workflow` entry carrying `workflow_file` and `api_port`.

The config `name` must match a yaml's `agent.name`. When it doesn't, the build
logs a warning, skips that image, and still exits 0.

## Step 4 — Build, then actually probe

```bash
ventis build          # requires Docker
```

A green build means the generator ran. It does not mean the project works — it
never imports your agent. On a real port this prints `Build complete.`, tags
both images, and exits 0 for a project whose agent cannot be imported at all.
Follow it by doing what the container will do:

```bash
cd docker_container/<AgentName>
python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('m', '<entrypoint basename>.py')
m = importlib.util.module_from_spec(spec); sys.modules['m'] = m
spec.loader.exec_module(m)
m.<AgentName>()          # the no-arg instantiation _load_agent performs
print('ok')
"
```

This turns the silent load-time failure into a visible one, and it is where the
two known walls surface: a third-party import failing because dependencies
cannot be declared, or a sibling-module import failing because an agent is
exactly one file. Both are described in `ventis-contract.md`.

When you hit a wall, report it with the exact `ModuleNotFoundError` and stop.
Inlining thousands of lines to route around it produces something nobody can
maintain, and it hides the signal that Ventis needs the fix.

## Traps, in the order they bite

| Symptom | Cause |
|---|---|
| `"No agent loaded"` on first request | anything at all — read the agent container's stdout, that is the only place the cause exists |
| A replica reports `healthy` but answers nothing | same — `healthy` is written before the agent loads and never revised |
| An agent's own stub is missing inside its container | the yaml basename equals the entrypoint basename; the implementation overwrote the stub |
| `NameError` importing a stub | a yaml `type` that isn't a builtin |
| `TypeError: unexpected keyword argument` | yaml `arguments[].name` ≠ the Python parameter name |
| `.value()` returns a `str` of a dict | expected — `json.loads` it |
| Redis holds `<coroutine object ...>` | the method is `async def`; make the signature sync, `asyncio.run` inside |
| The port is no faster than the original | calls fused with `.value()` — dispatch all, then resolve all |
| An agent is missing from the deployment | config `name` matched no yaml; the build warned and moved on |
| Debug code runs in production | the workflow is `exec`'d, so `__name__ == "__main__"` is true |
