# One port, end to end

A LangGraph email assistant, ported and validated, then deployed with explicit
approval. Read this for the shape of the decisions; the rules themselves are in
SKILL.md.

## Contents

- The source
- Decision 1: where to root the copy
- Decision 2: one agent, two methods
- The files
- The evidence

## The source

A single-file LangGraph app under `src/`, plus its own packages:

```text
src/email_assistant.py    triage_router + a ReAct loop (llm_call/tool_node)
src/prompts.py src/schemas.py src/utils.py src/tools/
pyproject.toml            package-dir = {"" = "src"}
.env                      OPENAI_API_KEY
```

Two graphs. Outer: `START -> triage_router -> (END | response_agent)`, routed by
a `Command(goto=...)`. Inner: `llm_call -> should_continue -> tool_node`, looping
until the model calls `Done`.

## Decision 1: where to root the copy

`email_assistant.py` imports `from tools import ...` and `from prompts import
...`, and `pyproject.toml` says `package-dir = {"" = "src"}`. So the import root
is `src/`, not the repository root:

```bash
python3 <skill_dir>/prepare.py src .car
```

This creates `.car/config/` and copies the contents of `src/` into `.car/app/`
with the standard source and credential exclusions.

Copying the repository root instead puts those modules at `/app/src/tools` while
`/app` is the only entry on `sys.path`. The build stays green, the replica
reports healthy, and the first request answers `No agent loaded` with
`No module named 'tools'` in the container log. The validator reports this as
V031 before any of that happens.

`pyproject.toml` was left out of the copy on purpose: this runtime runs no
editable install, and its `package-dir = {"" = "src"}` is false of a copy that
is already rooted at `src/`.

## Decision 2: one agent, two methods

The outer graph is framework control flow, so it became an `if` in the workflow.
The inner ReAct loop stayed inside one agent method: every turn needs the whole
message history, so splitting `llm_call` from `tool_node` would push a growing
message list through Redis for no parallelism.

The adapter is appended to the bottom of the copied `email_assistant.py`, so it
calls `triage_router`, `llm_call`, `should_continue` and `tool_node` as
module-level names. No prompt, tool, schema or model call is restated.

```python
class EmailAgent:
    def __init__(self):
        self.recursion_limit = int(os.environ.get("VENTIS_RECURSION_LIMIT", "25"))

    def triage(self, email_input: dict) -> dict:
        command = triage_router({"email_input": email_input, "messages": []})
        update = command.update or {}
        return {"goto": command.goto, **update}

    def respond(self, messages: list) -> dict:
        state = {"messages": add_messages([], messages)}
        for _ in range(self.recursion_limit):
            state["messages"] = add_messages(state["messages"], llm_call(state)["messages"])
            if should_continue(state) != "Action":
                return {"messages": messages_to_dict(state["messages"])}
            state["messages"] = add_messages(state["messages"], tool_node(state)["messages"])
        raise RuntimeError(f"agent did not call Done within {self.recursion_limit} turns")
```

`Command` and LangChain message objects are framework types, so they are
unpacked and serialized before they cross the boundary.

## The files

```text
.car/config/global_controller.yaml   EmailAgent + Workflow, env_file: .env
.car/config/email_agent.yaml         triage(email_input: dict), respond(messages: list)
.car/app/email_assistant.py          source + the adapter above
.car/app/email_workflow.py           the outer graph as an if; deploy(main, port=8080)
.car/app/prompts.py schemas.py utils.py tools/    untouched copies
```

`entrypoint: email_assistant.py` and `workflow_file: email_workflow.py`, both
relative to `.car/app`. The workflow imports the agent from its entrypoint --
`from email_assistant import EmailAgent` -- which is the one module the build
replaces with a stub.

The platform sends `{query: string}` only, so the four email fields ride inside
`query` as JSON and the workflow unpacks them. The adapter returns a dict; the
runtime encodes it once for transport, so the workflow decodes the Future once
and returns an ordinary dict without another `json.dumps`:

```python
def main(query: str) -> dict:
    email = json.loads(query)
    triage = json.loads(agent.triage(email_input=email).value())
    if triage["goto"] == "END":
        return triage
    return json.loads(agent.respond(messages=triage["messages"]).value())
```

`GET /status/<request_id>` hands that result back under `result`.

## The evidence

```text
validate.py .car                 0 errors; porting workflow stopped
user approval                    yes, run deployment
canyonos deploy                  build complete; 2 replicas ready
POST /main                       202 {"request_id": ...}
GET  /status/<id>                status: error, 401 from OpenAI
```

The last line is the interesting one. The agent log showed the request arriving
over gRPC (`route_to: :8000`, `function: triage`), the agent loading, and the
source's own model call returning 401 on an expired key. A source-level failure
behind a working boundary still closes the port: record it as such rather than
calling the port broken.
