import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stubs"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "grpc_stubs"))

from deploy import deploy
from smoke_agent_stub import SmokeAgentStub


def main(name: str = "world"):
    reply = SmokeAgentStub().ping(name=name)
    return {"reply": reply.value()}


deploy(main, port=8080)
