r"""Ventis workflow for the map-reduce joke writer.

This file is where the graph went. `generate_topics -> continue_to_jokes ->
generate_joke x N -> best_joke` is not a compiled StateGraph any more; it is the
three statements below, and the `Send` fan-out is N calls dispatched across
JokeAgent's replicas.

The function is `main` and its one argument is `query` because the deployment
platform's test endpoint posts to a hardcoded /main with a strictly validated
{query: string} body. Ventis would serve any name and any kwargs -- the route is
the function's __name__ and the body is splatted in -- so nothing here fails if
you rename it; it just stops being reachable through the platform.

  curl -X POST http://localhost:8080/main \
       -H 'Content-Type: application/json' -d '{"query": "animals"}'
  curl http://localhost:8080/status/<request_id>
"""

import json

from deploy import deploy
from agents.joke_agent import JokeAgent


def main(query):
    """Route: POST /main  {"query": "<the topic to write jokes about>"}"""
    agent = JokeAgent()

    # Node 1: one call, and the fan-out width comes out of it. The agent's own
    # parameter is still `topic` -- that name is bound by joke_agent.yaml and the
    # source's node, and only the workflow's entry point is pinned to `query`.
    subjects = json.loads(agent.generate_topics(topic=query).value())["subjects"]

    # `continue_to_jokes`, re-expressed. Every call is dispatched before any
    # of them is resolved -- .value() blocks, so fusing these two lines into one
    # comprehension would run the jokes one after another. It would not error;
    # the fan-out would just be gone, and with it the reason to be on Ventis.
    futures = [agent.generate_joke(subject=s) for s in subjects]
    written = [json.loads(f.value()) for f in futures]

    # `Annotated[list, operator.add]`, re-expressed: the reducer that merged N
    # single-joke lists back into one list was part of the graph, not of a node.
    written_jokes = [joke for result in written for joke in result["jokes"]]

    # Node 3: the reduce. `list` in the yaml is what lets this argument through.
    best = json.loads(agent.best_joke(topic=query, jokes=written_jokes).value())

    return {
        "topic": query,
        "subjects": subjects,
        "jokes": written_jokes,
        "best_selected_joke": best["best_selected_joke"],
    }


# This file is exec'd, not imported, so __name__ == "__main__" here and any
# `if __name__ == "__main__":` block would run in production. deploy() blocks
# on app.run(); nothing after it executes.
deploy(main, port=8080)
