"""Ventis entrypoint for the LangGraph email assistant.

Nothing here restates the project. The triage prompt, the response agent, its
tools and the LLM binding all live in src/ and are reached with an import.

LangGraph owns the control flow, but there is no orchestration to re-express:
one agent holds the whole graph. This file only turns `invoke` into a method
Ventis can call, and hands back something Redis can hold.
"""

from langchain_core.messages import messages_to_dict

# The source tree, untouched.
from email_assistant import email_assistant


class EmailAssistant(object):
    """The compiled graph, exposed under the class name `agent.name` declares."""

    # No constructor arguments -- LocalController does `EmailAssistant()`.

    def run(self, email_input: dict) -> dict:
        """Triage one email and draft a reply when the triage calls for one.

        Synchronous by signature -- the executor calls this with no `await`, and
        returning a coroutine would put `<coroutine object ...>` into Redis.
        """
        # The initial state goes in positionally, which keeps LangGraph's own
        # parameter name (`input`) out of the agent yaml.
        result = email_assistant.invoke({"email_input": email_input})

        # _execute_locally does json.dumps() on a dict result. The graph's state
        # is a MessagesState, so `messages` holds LangChain message objects and
        # json.dumps raises on them. Use the framework's own serializer rather
        # than a hand-rolled one -- `m.content` is empty on a tool call.
        #
        # On the ignore and notify branches the graph never writes `messages`,
        # so it comes back empty and classification_decision is the answer.
        return {
            "classification_decision": result.get("classification_decision"),
            "messages": messages_to_dict(result.get("messages", [])),
        }
