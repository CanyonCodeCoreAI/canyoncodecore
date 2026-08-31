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
"""

import json
import operator
import os
import re
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


def _ask(prompt, schema, max_tokens):
    """One converse() call, validated into `schema`.

    Raising on a bad reply is deliberate. A node that returned a default would
    put a plausible-looking wrong answer into the state, and the reduce step
    downstream indexes into the jokes list by an id the model chose -- a silent
    default there picks the wrong joke instead of failing.
    """
    response = call_bedrock(
        model_id=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inference_config={"maxTokens": max_tokens, "temperature": 0.0},
        region=REGION,
    )
    text = response["output"]["message"]["content"][0]["text"]
    if not text:
        raise ValueError("joke_writer: LLM returned no output.")
    try:
        return schema(**_extract_json(text))
    except (ValidationError, TypeError) as exc:
        raise ValueError(
            f"joke_writer: {schema.__name__} not satisfied by model output: {text!r}"
        ) from exc


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

def generate_topics(state: OverallState):
    prompt = subjects_prompt.format(topic=state["topic"])
    response = _ask(prompt, Subjects, max_tokens=300)
    return {"subjects": response.subjects}

class JokeState(TypedDict):
    subject: str

class Joke(BaseModel):
    joke: str

def generate_joke(state: JokeState):
    prompt = joke_prompt.format(subject=state["subject"])
    response = _ask(prompt, Joke, max_tokens=300)
    return {"jokes": [response.joke]}

def best_joke(state: OverallState):
    jokes = "\n\n".join(state["jokes"])
    prompt = best_joke_prompt.format(topic=state["topic"], jokes=jokes)
    response = _ask(prompt, BestJoke, max_tokens=100)
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
