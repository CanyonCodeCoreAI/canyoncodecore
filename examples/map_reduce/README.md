# Map-Reduce

Trimmed fork of [langchain-ai/langchain-academy](https://github.com/langchain-ai/langchain-academy)
at `fa15bec` (`module-4/studio/map_reduce.py`, MIT — see `LICENSE`), kept as a
target for the `porting-to-ventis` skill.

`map_reduce.py` is unmodified. See [Provenance](#provenance) for what was left
behind.

## Overview

A LangGraph map-reduce: given a topic, the graph splits it into sub-topics,
writes one joke per sub-topic in parallel, then picks the best of them.

1. `generate_topics` — one LLM call, structured output into `Subjects`, turns
   the topic into three sub-topics.
2. `generate_joke` — one LLM call per sub-topic. `continue_to_jokes` emits a
   `Send` per subject, so this node runs N times per request with no shared
   state between the runs. `jokes` is an `Annotated[list, operator.add]`, which
   is how the N results merge back into one state.
3. `best_joke` — one LLM call over every joke, structured output into
   `BestJoke`, returns the winner by index.

## Details

| Feature | Description |
| --- | --- |
| **Framework** | LangGraph (`StateGraph`, `Send`) |
| **Interaction type** | Single turn |
| **Agent type** | Map-reduce over one node |
| **Components** | No tools, no retrieval, no external API — every node is an LLM call |
| **Model** | `gpt-4o` via `langchain-openai` |

### Architecture

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

## Running the original

The four packages `map_reduce.py` imports, pinned as langchain-academy's own
`requirements.txt` pins them:

```shell
pip install 'langgraph' 'langchain-core>=1.2.28' 'langchain-openai>=1.1.14' pydantic
cp .env.example .env    # then put a real key in it
```

```python
from map_reduce import graph

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
| `TAVILY_API_KEY` in `.env.example` | Belongs to the two graphs that are not here. |

Nothing was added: there is no `pyproject.toml`, `setup.py` or
`requirements.txt` at this root, exactly as upstream has none for module-4.
