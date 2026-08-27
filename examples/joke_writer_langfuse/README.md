# Joke Writer

A LangGraph map-reduce, ported to Ventis. Derived from
[langchain-ai/langchain-academy](https://github.com/langchain-ai/langchain-academy)
at `fa15bec` (`module-4/studio/map_reduce.py`, MIT — see `LICENSE`).

Unlike the other targets in `examples/`, **the source here is not unmodified**.
`map_reduce.py` built a `ChatOpenAI` at module scope, and at the time of the port
an agent container had no way to carry an `OPENAI_API_KEY` — the port was blocked
at the credential wall until the model call was rewritten onto Bedrock. That wall
is gone now: `env_file` puts any key in the container. The Bedrock rewrite stayed
anyway, and [What the port cost](#what-the-port-cost) is honest about what that
means.

## Overview

Given a topic, the graph splits it into sub-topics, writes one joke per
sub-topic in parallel, then picks the best of them.

1. `generate_topics` — one LLM call, turns the topic into three sub-topics,
   validated into `Subjects`.
2. `generate_joke` — one LLM call per sub-topic. `continue_to_jokes` emits a
   `Send` per subject, so this node runs N times per request with no shared
   state between the runs. `jokes` is an `Annotated[list, operator.add]`, which
   is how the N results merge back into one state.
3. `best_joke` — one LLM call over every joke, returns the winner by index.

```
        START
          |
   generate_topics          1 call
          |
   continue_to_jokes        Send x N
     /    |    \
  joke   joke   joke        N calls, no shared state
     \    |    /
      best_joke             1 call
          |
         END
```

### Why this one

It is the smallest project in reach whose control flow does something a single
process cannot: `Send` fans out to N independent calls per request. Everything
else about it is deliberately boring — four packages, no tools, no external
service, one API key.

## The port

| File | What it holds |
| --- | --- |
| `joke_writer.py` | The source. Three prompts, two schemas, three nodes, and the graph — still compiled, never executed under Ventis. |
| `agents/joke_agent.py` | `JokeAgent`. Three methods, each calling the source's node with the node's own state dict. Imports `joke_writer`; restates nothing. |
| `agents/joke_agent.yaml` | The three nodes declared as three functions on one agent. |
| `workflow/joke_workflow.py` | Where the graph went — the edges, the `Send` fan-out and the `operator.add` reducer, re-expressed as ordinary Python. |
| `config/global_controller.yaml` | `JokeAgent` at `replicas: 3`, plus the workflow. |
| `config/policy.yaml` | Default-allow for the two services. Not optional — a missing file kills `ventis deploy`. |

Two decisions worth naming:

**One agent, not three.** `generate_topics` and `best_joke` run once per request
and have no resource profile of their own. Splitting them out would buy two more
images and two more Redis round trips. What is hoisted is the fan-out, and that
is a workflow concern.

**The graph is not the port.** `StateGraph`, `Send` and the `Annotated[list,
operator.add]` reducer are control flow owned by the LangGraph runtime, and
Ventis has no runtime to execute them. The workflow dispatches N
`generate_joke` calls across the three replicas and concatenates the results
itself. Every call is dispatched before any is resolved — `.value()` blocks, so
fusing the two lines into one comprehension would silently serialize the fan-out
and remove the reason to be on Ventis at all.

## What the port cost

This is no longer upstream's model stack. `ChatOpenAI` and
`with_structured_output` are gone; `ventis.llm.bedrock.call_bedrock` is the raw
converse API, so each node asks for JSON in its prompt and validates the reply
through the same pydantic schema upstream used. `_extract_json` exists only
because `with_structured_output` used to do that work.

That rewrite is not something the `porting-to-ventis` skill should do on a
user's project — it is the credential wall, and the skill's instruction is to
report it. It was done here deliberately, so that this example is one that
actually deploys.

**It would not be necessary today.** The rewrite bought one thing: boto3 builds
no client at import, so the agent could be *loaded* with no secret in the
container, back when `_launch_locally` passed five `-e` flags and all five were
`VENTIS_*`. `env_file` removes that constraint — an `OPENAI_API_KEY` now reaches
a container as readily as a Bedrock one, and upstream's `ChatOpenAI` at module
scope would import fine. What the rewrite still buys is narrower: a module-scope
client turns a missing key into `"No agent loaded"`, while a per-call one turns
it into a real error on `/status`. Worth knowing, not worth a rewrite.

The example stays on Bedrock because it is the model call that has been end-to-end
verified here, and because `ventis/llm/bedrock.py` is where Ventis writes per-call
token telemetry onto the future.

## Running it

Copy `.env.example` to `.env` and put a Bedrock API key in it:

```shell
cp .env.example .env
$EDITOR .env          # AWS_BEARER_TOKEN_BEDROCK=bedrock-api-key-...
```

`config/global_controller.yaml` points `env_file:` at that file, and every
container gets it as `docker run --env-file`. Nothing in this project reads the
variable: botocore matches the name against `bedrock-runtime`'s signingName and
switches the client from SigV4 to bearer auth on its own, so
`ventis/llm/bedrock.py` still builds a plain `boto3.client("bedrock-runtime")`.
An IAM access key instead of the bearer token works the same way.

`.env` is gitignored and excluded from the build context — the key is in the
container's environment and not in the image. Deploy checks the path before it
launches anything, so a missing `.env` is one error line rather than three
replicas that come up and fail every request.

```shell
ventis build
ventis deploy
```

```shell
curl -X POST http://localhost:8080/main \
     -H 'Content-Type: application/json' -d '{"query": "animals"}'
curl http://localhost:8080/status/<request_id>
```

```json
{"request_id": "cb6cb62d...", "status": "done", "result": {
  "topic": "animals",
  "subjects": ["Wildlife Conservation", "Animal Behavior", "Endangered Species"],
  "jokes": ["...", "...", "..."],
  "best_selected_joke": "Why did the chimpanzee go to the doctor? Because it was going bananas!"
}}
```

`BEDROCK_MODEL_ID` and `AWS_REGION` (see `.env.example`) have defaults in
`joke_writer.py`; neither is a secret. The region has to match the one the key
was issued for.

### Running the source outside Ventis

`joke_writer.py` imports `ventis.llm.bedrock` first and falls back to the flat
`bedrock` copy an agent image gets, so the compiled graph still runs on its own
from a checkout of this repo:

```shell
pip install -e ../..                              # the ventis package
pip install langgraph pydantic typing_extensions boto3
```

```python
from joke_writer import graph

graph.invoke({"topic": "animals"})
```

## Provenance

Taken from `module-4/studio/`, which holds four unrelated graphs sharing one
directory. Only `map_reduce.py` and its license are here.

| Left behind | Why |
| --- | --- |
| `parallelization.py`, `research_assistant.py`, `sub_graphs.py` | Other graphs in the same studio directory. The first two also need a Tavily key and Wikipedia. |
| `langgraph.json` | Registers all four graphs and points at `./.env`; a trimmed copy would only be useful for LangGraph Studio. |
| The module-4 notebooks | Teaching material for the same code. |
| `OPENAI_API_KEY`, `TAVILY_API_KEY` in `.env.example` | The first belongs to a model call that is no longer here; the second to the two graphs that are not here. |

Nothing was added at the project root: there is no `pyproject.toml`, `setup.py`
or `requirements.txt`, exactly as upstream has none for module-4. That is why
`config/global_controller.yaml` has to declare `requirements:` by hand.
