"""Ventis entrypoint for the map-reduce joke writer.

Nothing here restates the project. The three prompts, the two schemas and the
Bedrock binding all live in `joke_writer.py` and are reached with an import --
the whole project tree is in the image.

What could not be reused is the graph itself. `StateGraph`, the `Send` in
`continue_to_jokes` and the `Annotated[list, operator.add]` reducer are control
flow owned by the LangGraph runtime, and Ventis has no runtime to execute them.
That wiring is re-expressed as ordinary Python in workflow/joke_workflow.py,
where the fan-out becomes N dispatched calls across this agent's replicas. The
nodes those edges connected are imported, unchanged.

The module is imported whole rather than by name so that
`joke_writer.generate_joke` inside a method named `generate_joke` reads as what
it is: the source's node.
"""

# The source tree. Importing it reads BEDROCK_MODEL_ID and AWS_REGION, imports
# bedrock.py (which builds a RedisClient at module scope) and compiles the graph
# -- but it constructs no API client, so the import needs no credential.
#
# The credential arrives by a different road: `env_file` in
# config/global_controller.yaml hands the container a .env holding
# AWS_BEARER_TOKEN_BEDROCK, and botocore picks that name up by itself. Nothing
# here or in joke_writer.py names it.
#
# Constructing no client at import is no longer what makes this agent loadable --
# env_file would carry a key to a module-scope client too. It only changes the
# failure: a missing key is an error on /status rather than "No agent loaded".
import joke_writer


class JokeAgent(object):
    """The graph's nodes, exposed under the class name `agent.name` declares."""

    # No constructor arguments -- LocalController does `JokeAgent()`. The model
    # id and region are the source's own module-level constants, read from the
    # environment there; there is nothing to configure here.

    def generate_topics(self, topic: str) -> dict:
        """Split a topic into sub-topics. Returns {"subjects": [...]}.

        Synchronous by signature -- the executor calls this with no `await`, and
        returning a coroutine would put `<coroutine object ...>` into Redis.
        """
        # The node's own state dict goes in, the node's own return comes out.
        # Both hold nothing but str and list, so the executor's json.dumps is
        # happy without a serializer -- unlike a graph that hands back messages.
        return joke_writer.generate_topics({"topic": topic})

    def generate_joke(self, subject: str) -> dict:
        """Write one joke about one subject. Returns {"jokes": ["..."]}.

        The single-element list is the node's own shape: it is what
        `Annotated[list, operator.add]` merged N of. The workflow does that
        concatenation now.
        """
        return joke_writer.generate_joke({"subject": subject})

    def best_joke(self, topic: str, jokes: list) -> dict:
        """Pick the winner. Returns {"best_selected_joke": "..."}."""
        return joke_writer.best_joke({"topic": topic, "jokes": jokes})
