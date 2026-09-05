# Example Workflow
# This file demonstrates how to call agent stubs and deploy as a REST API.
#
# After running `canyonos build` and `canyonos deploy`:
#   curl -X POST http://localhost:8080/main -H 'Content-Type: application/json' -d '{"query": "World"}'
#   curl http://localhost:8080/status/<request_id>

import sys
import os

# These path inserts are needed when running inside a Docker container
# where all files are copied flat into /app/.
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

from deploy import deploy
from agents.example_agent import ExampleAgent


def main(query: str = "World"):
    agent = ExampleAgent()
    greeting = agent.hello(name=query)
    return {"greeting": greeting.value()}


deploy(main, port=8080)
