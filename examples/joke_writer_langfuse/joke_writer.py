"""Map-reduce joke writer.

Derived from langchain-ai/langchain-academy `module-4/studio/map_reduce.py`
(MIT, see LICENSE). The graph shape, the three prompts and the two schemas are
upstream's. The model call is not: upstream builds a `ChatOpenAI` at module
scope, and when this was ported nothing could carry an OPENAI_API_KEY into an
agent container. Bedrock reaches the model through boto3, which builds no client
at import, so the same code loaded with no secret injected.

`env_file` has since removed that constraint -- the key now travels to the
container in a .env and botocore reads AWS_BEARER_TOKEN_BEDROCK on its own. The
rewrite stayed regardless; README.md says what that costs.

`with_structured_output` went with it. `call_bedrock` is the raw converse API, so
each node asks for JSON in the prompt and validates the reply through the same
pydantic schema upstream used.

Tracing
-------
One run is one Langfuse trace. `write_jokes()` is the traced entry point: it
opens the root span, and the three nodes nest under it as spans, each wrapping
the `generation` that records its Bedrock call -- model, prompt, raw reply and
token usage.

Instrumented by hand rather than through Langfuse's LangChain callback handler.
The handler traces what LangChain runs, and the model call here is boto3's
converse API, so it would draw the graph and leave every generation empty. Doing
it in the nodes also means the tracing survives the graph: an agent that imports
`generate_joke` and calls it directly -- which is what a Ventis port of this
project does -- still gets its span and its generation.

Tracing is optional at runtime. With no LANGFUSE_* in the environment the SDK
disables itself, every observation below becomes a no-op and the run returns
what it always returned -- it is not silent about it, though: the SDK logs one
"client will be disabled" warning per traced call site. `LANGFUSE_TRACING_ENABLED
=false` removes about half of them, and `logging.getLogger("langfuse")` is where
the rest live. See README.md.
"""

import json
import operator
import os
import re
import sys
from typing import Annotated

from typing_extensions import TypedDict

from pydantic import BaseModel, ValidationError

from langgraph.constants import Send
from langgraph.graph import END, StateGraph, START

# Ventis copies bedrock.py flat into every agent image; the package path is for
# running this module outside a container.
try:
    from ventis.llm.bedrock import call_bedrock
except ImportError:
    from bedrock import call_bedrock

# Importing langfuse constructs no client and reads no credential -- `get_client()`
# does, on its first call, which happens inside a node. Anything that loads a
# .env before calling one therefore still gets a configured client, whatever the
# import order here.
#
# With no LANGFUSE_* in the environment the SDK disables itself and every
# observation below becomes a no-op, so the module runs untraced rather than
# refusing to run.
from langfuse import get_client, observe

_langfuse = None


def _client():
    """`get_client()`, resolved once.

    Unconfigured, `get_client()` has no public key to file a client under, so it
    builds a fresh disabled one -- and logs a fresh "client will be disabled"
    warning -- on every call. Holding the first one turns eighteen of those
    lines per run into one.
    """
    global _langfuse
    if _langfuse is None:
        _langfuse = get_client()
    return _langfuse

# Prompts we will use. Upstream's, plus the JSON instruction that
# `with_structured_output` used to add on our behalf.
subjects_prompt = """Generate a list of 3 sub-topics that are all related to this overall topic: {topic}.
Respond with JSON only, no prose: {{"subjects": ["...", "...", "..."]}}"""
joke_prompt = """Generate a joke about {subject}.
Respond with JSON only, no prose: {{"joke": "..."}}"""
best_joke_prompt = """Below are a bunch of jokes about {topic}. Select the best one! Return the ID of the best one, starting 0 as the ID for the first joke. Jokes: \n\n  {jokes}
Respond with JSON only, no prose: {{"id": 0}}"""

# LLM. Both are read once at import; the container gets them from its
# environment, and neither is a secret.
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "meta.llama3-8b-instruct-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _extract_json(text):
    """Pull the first JSON object out of a model reply.

    Even told to answer with JSON only, a model wraps it in a ```json fence or
    prefaces it with a sentence. Upstream never needed this because
    `with_structured_output` handled it; the converse API does not.
    """
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost braced span.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"joke_writer: no JSON in model output: {text!r}")
    return json.loads(match.group(0))


def _usage_details(response):
    """Bedrock's `usage` block, keyed the way Langfuse prices generations.

    `total` is sent rather than left to be derived: Langfuse derives it as the
    sum of every usage type present, which double-counts the moment a cache
    figure is included. Bedrock's own totalTokens is input + output.
    """
    usage = response.get("usage") or {}
    details = {
        "input": usage.get("inputTokens"),
        "output": usage.get("outputTokens"),
        "total": usage.get("totalTokens"),
        "cache_read_input_tokens": usage.get("cacheReadInputTokens"),
        "cache_write_input_tokens": usage.get("cacheWriteInputTokens"),
    }
    return {k: v for k, v in details.items() if v is not None}


def _ask(prompt, schema, max_tokens, name):
    """One converse() call, recorded as a `generation` and validated into `schema`.

    Raising on a bad reply is deliberate. A node that returned a default would
    put a plausible-looking wrong answer into the state, and the reduce step
    downstream indexes into the jokes list by an id the model chose -- a silent
    default there picks the wrong joke instead of failing.

    `name` is the generation's name in Langfuse and belongs to the call site,
    not to this function: all three calls are the same converse() and telling
    them apart in the UI, in a dashboard filter or in an LLM-as-a-judge target
    is the whole point of naming them separately.
    """
    with _client().start_as_current_observation(
        as_type="generation",
        name=name,
        model=MODEL_ID,
        # A flat role/content list is what Langfuse renders as a conversation.
        # Bedrock's own {"content": [{"text": ...}]} shape renders as raw JSON.
        input=[{"role": "user", "content": prompt}],
        model_parameters={"maxTokens": max_tokens, "temperature": 0.0},
        metadata={"provider": "bedrock", "api": "converse", "region": REGION},
    ) as generation:
        try:
            response = call_bedrock(
                model_id=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inference_config={"maxTokens": max_tokens, "temperature": 0.0},
                region=REGION,
            )
        except Exception as exc:
            # The span would carry the exception either way; the level is what
            # makes it filterable next to the replies that came back malformed.
            generation.update(
                level="ERROR", status_message=f"{type(exc).__name__}: {exc}"
            )
            raise

        text = response["output"]["message"]["content"][0]["text"]
        generation.update(
            output=text,
            usage_details=_usage_details(response),
            metadata={
                "stop_reason": response.get("stopReason"),
                "latency_ms": (response.get("metrics") or {}).get("latencyMs"),
                "request_id": (response.get("ResponseMetadata") or {}).get("RequestId"),
            },
        )

        if not text:
            generation.update(level="ERROR", status_message="empty completion")
            raise ValueError("joke_writer: LLM returned no output.")
        try:
            return schema(**_extract_json(text))
        except (ValidationError, TypeError) as exc:
            generation.update(
                level="ERROR",
                status_message=f"{schema.__name__} not satisfied by model output",
            )
            raise ValueError(
                f"joke_writer: {schema.__name__} not satisfied by model output: {text!r}"
            ) from exc
        except ValueError:
            # _extract_json found nothing to parse. The raw reply is already on
            # the generation's output, which is where you go to see why.
            generation.update(level="ERROR", status_message="no JSON in model output")
            raise


# Define the state
class Subjects(BaseModel):
    subjects: list[str]

class BestJoke(BaseModel):
    id: int

class OverallState(TypedDict):
    topic: str
    subjects: list
    jokes: Annotated[list, operator.add]
    best_selected_joke: str

@observe(name="generate-topics")
def generate_topics(state: OverallState):
    # Set by hand so the span's input is what the node was actually given. Left
    # to `@observe` it is the call shape, {"state": {...}}, carrying whatever
    # else the graph has accumulated in the state by the time the node runs.
    _client().update_current_span(input={"topic": state["topic"]})
    prompt = subjects_prompt.format(topic=state["topic"])
    response = _ask(prompt, Subjects, max_tokens=300, name="split-topic")
    return {"subjects": response.subjects}

class JokeState(TypedDict):
    subject: str

class Joke(BaseModel):
    joke: str

@observe(name="generate-joke")
def generate_joke(state: JokeState):
    _client().update_current_span(input={"subject": state["subject"]})
    prompt = joke_prompt.format(subject=state["subject"])
    response = _ask(prompt, Joke, max_tokens=300, name="write-joke")
    return {"jokes": [response.joke]}

@observe(name="select-best-joke")
def best_joke(state: OverallState):
    jokes = "\n\n".join(state["jokes"])
    # `subjects` is in the state by now and is not an input to this decision.
    _client().update_current_span(
        input={"topic": state["topic"], "jokes": state["jokes"]}
    )
    prompt = best_joke_prompt.format(topic=state["topic"], jokes=jokes)
    response = _ask(prompt, BestJoke, max_tokens=100, name="judge-jokes")
    if not 0 <= response.id < len(state["jokes"]):
        raise ValueError(
            f"joke_writer: model chose joke {response.id} of {len(state['jokes'])}."
        )
    return {"best_selected_joke": state["jokes"][response.id]}

def continue_to_jokes(state: OverallState):
    return [Send("generate_joke", {"subject": s}) for s in state["subjects"]]

# Construct the graph: here we put everything together to construct our graph
graph_builder = StateGraph(OverallState)
graph_builder.add_node("generate_topics", generate_topics)
graph_builder.add_node("generate_joke", generate_joke)
graph_builder.add_node("best_joke", best_joke)
graph_builder.add_edge(START, "generate_topics")
graph_builder.add_conditional_edges("generate_topics", continue_to_jokes, ["generate_joke"])
graph_builder.add_edge("generate_joke", "best_joke")
graph_builder.add_edge("best_joke", END)

# Compile the graph
graph = graph_builder.compile()


@observe(name="write-jokes")
def write_jokes(topic: str) -> dict:
    """Run the graph once. The traced entry point.

    `graph.invoke({"topic": ...})` still works and is still what the graph is
    for; it just has no observation of its own to hang the five node spans
    under, so each one opens a trace of its own and a single run arrives in
    Langfuse as five unrelated traces. This wrapper is that missing root.

    The trace's input and output are this span's, and they are what the tracing
    table shows and what a dataset experiment compares across runs. The output
    is the returned state; the input is set by hand, because what `@observe`
    captures unaided is the call shape -- {"args": ["animals"], "kwargs": {}} --
    rather than the topic.
    """
    _client().update_current_span(input={"topic": topic})
    return graph.invoke({"topic": topic})


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "animals"
    print(json.dumps(write_jokes(topic), indent=2))
    # Short-lived process: the SDK batches in the background, so without this
    # the interpreter can exit before the spans are shipped. `flush()` blocks
    # until the queue is drained.
    _client().flush()
