r"""CanyonOS Core workflow for the map-reduce joke writer.

This file is where the graph went. `generate_topics -> continue_to_jokes ->
generate_joke x N -> best_joke` is not a compiled StateGraph any more; it is the
three statements below, and the `Send` fan-out is N calls dispatched across
JokeAgent's replicas.

  curl -X POST http://localhost:8080/main \
       -H 'Content-Type: application/json' -d '{"query": "animals"}'
  curl http://localhost:8080/status/<request_id>
"""

import json

from deploy import deploy
from joke_writer import JokeAgent


def main(query):
    """Route: POST /main  {"query": "<the topic to write jokes about>"}"""
    agent = JokeAgent()

    subjects = json.loads(agent.generate_topics(topic=query).value())["subjects"]

    futures = [agent.generate_joke(subject=s) for s in subjects]
    written = [json.loads(f.value()) for f in futures]
    written_jokes = [joke for result in written for joke in result["jokes"]]

    best = json.loads(agent.best_joke(topic=query, jokes=written_jokes).value())

    return {
        "topic": query,
        "subjects": subjects,
        "jokes": written_jokes,
        "best_selected_joke": best["best_selected_joke"],
    }


deploy(main, port=8080)
