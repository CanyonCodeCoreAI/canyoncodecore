# Route model calls through `llm_proxy`

**When:** the source calls an OpenAI, Anthropic, or Bedrock model API. Proxy
routing is required for every such deployment by default -- treat finding the
call during the survey as the trigger, not a pre-existing `llm_proxy`
reference or an env hook already wired into the deployment.

**Output:** the proxy's environment settings written into the deployment's own
env files, verified routing, and a clear blocker for unsupported call shapes.

## Contents

- Preserve provider protocols
- Where the proxy runs
- Set every spelling, not the one you expect
- Write the env files, do not advise them
- A source with no env hook needs one in the `.car/app` copy
- Confirm the route from inside the container
- Supported call shape
- Credential behavior

## Preserve provider protocols

The proxy redirects provider endpoints; it does not convert providers. Keep the
source SDK, model ID, request body, and response parsing unchanged.

## Where the proxy runs

`canyonos deploy` starts one `llm_proxy` subprocess **inside every agent and
workflow container**, bound to that container's own loopback
(`local_controller.py::_start_llm_proxy` sets `PROXY_HOST=127.0.0.1`,
`PROXY_PORT=8081`). Every container's boot log prints `Started LLM proxy on
127.0.0.1:8081`. There is nothing to start on the developer's machine and no
host-side proxy to reach.

- `127.0.0.1:8081` inside a container is that container's own proxy. It is the
  address to use, and the only one that works.
- `host.docker.internal` does not resolve inside these containers, and on the
  host 8081 is the CanyonOS dashboard. Pointing the port at either address
  sends every model call to the wrong place or to nothing. Never advise it.
- Bedrock is already routed: every runtime passes
  `-e AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://127.0.0.1:8081/bedrock`, which
  beats `--env-file`. OpenAI and Anthropic get no such injection -- their base
  URLs are the port's job, below.

## Set every spelling, not the one you expect

Each SDK generation reads a different base-URL variable, and a wrapper library
reads a different one from the SDK it wraps. Set only the name this reference
used to give and the container reaches the real provider with a placeholder key:
a 401 that reads like a broken port, after validation and the deployment build
have passed. Set all of them for whichever providers the source uses:

```dotenv
OPENAI_BASE_URL=http://127.0.0.1:8081/openai/v1
OPENAI_API_BASE=http://127.0.0.1:8081/openai/v1
ANTHROPIC_BASE_URL=http://127.0.0.1:8081/anthropic
ANTHROPIC_API_URL=http://127.0.0.1:8081/anthropic
ANTHROPIC_API_BASE=http://127.0.0.1:8081/anthropic
```

Which name actually wins, for when a call still escapes:

| Caller | Reads |
|---|---|
| `openai` SDK | `OPENAI_BASE_URL` |
| `langchain_openai` | `OPENAI_API_BASE` |
| `llama_index.llms.openai`, `llama_index.embeddings.openai` | `OPENAI_API_BASE` only -- `resolve_openai_credentials()` never looks at `OPENAI_BASE_URL` |
| `anthropic` SDK | `ANTHROPIC_BASE_URL` |
| `langchain_anthropic` | `ANTHROPIC_API_URL` first, `ANTHROPIC_BASE_URL` as fallback |
| LiteLLM | `ANTHROPIC_API_BASE` |

Some SDKs refuse to initialize without caller credentials, and the proxy reads
its own upstream key from the same container environment, so the port's
`env_file` is where `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` belong.

## Write the env files, do not advise them

Those lines go into two files at the **application root** -- the directory
`canyonos` runs from, beside `.car`, never the copy inside it:

- `.env`, which `env_file` names and the runtime hands to every container;
- `.env.example`, so the shape survives for whoever deploys this next. Add the
  key names the source reads here too, with empty values.

Append them. **Never read `.env`**: it holds the developer's real keys, and
nothing in this step needs to know one. Appending blind is safe, and is in fact
the point -- a repeated assignment wins over the earlier one in both
`python-dotenv` and `docker --env-file`, so a stale base URL already in the file
loses to the line you add. `.env.example` carries no secrets, so read that one
and add only what it lacks. Do not write an empty `OPENAI_API_KEY=` into `.env`:
an empty value reaches the container and some SDKs accept it, turning a missing
credential into a 401 that reads like a broken port.

This is a step, not a recommendation. Do not ask for approval first, and do not
close the port by reporting that the `.env` "should" point at the proxy: a
report that says so while the file still says otherwise is the failure this
section exists to prevent. `git status` will show `.env.example`; name it in the
handoff.

## A source with no env hook needs one in the `.car/app` copy

Some sources build the HTTP call themselves -- `urllib.request` against a module
constant like `API = "https://api.openai.com/v1/responses"` -- and read no
base-URL variable at all. No `env_file` can reach that call, so give it the hook
it lacks, in the copy under `.car/app`:

```python
API = (
    os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    + "/responses"
)
```

The edit lands in the artifact, never in the developer's tree, and it changes
only the address the call dials -- request body, model ID and response parsing
stay exactly as the source wrote them, which is what **Preserve provider
protocols** above asks for. Default to the original constant so the module still
behaves identically outside a CanyonOS container. The source-integrity boundary
in [preparation.md](preparation.md) protects the original tree, and inside the
copy it protects prompts, tools, schemas, model calls and node bodies (see
[adapter.md](adapter.md)) -- not an endpoint string.

This is not a blocker. Do not hand the container a real upstream credential
instead, and do not stop the port here.

Detect it before deploying: grep the source for the provider hostname. A literal
`api.openai.com` or `api.anthropic.com` outside a comment means the call bypasses
the SDK's base-URL resolution entirely and needs this edit.

## Confirm the route from inside the container

Confirm the route rather than assuming it. The proxy logs one line per
forwarded call, so the container's own log is the evidence:

```bash
docker logs <container> | grep -E "LLM proxy|8081"
```

A request that succeeds with no proxy line went straight to the provider: the
SDK read a base-URL name the `env_file` does not set (table above), not a proxy
fault. `curl http://127.0.0.1:8081/healthz` **from inside that container**
proves provider registration and Flask availability, nothing about upstream
credentials.

Each container proxies only for itself, so this holds unchanged on EC2: no
cross-host proxy address is ever needed.

## Supported call shape

- OpenAI and Anthropic non-streaming HTTP calls are buffered and forwarded.
- Bedrock `invoke`, `converse`, `invoke-with-response-stream`, and
  `converse-stream` are all reissued through the proxy's boto3 client. The two
  streaming ops are decoded by boto3 and re-encoded back into the AWS
  event-stream wire format, so the caller's own boto3 client decodes them
  exactly as if it had hit Bedrock directly.

Streaming splits by how the source **consumes** the response, not by whether a
`streaming` flag is set. For OpenAI and Anthropic, the proxy buffers, so it
forwards anything that reads a complete response and breaks anything that
reads tokens as they arrive (Bedrock's `converse-stream` and
`invoke-with-response-stream` are the exception -- see above, they stream
end to end):

- `ChatOpenAI(streaming=True)` reached through `.invoke()` works. LangChain
  drains the stream inside the call and returns one message; the proxy sees an
  ordinary buffered request. Verified end to end against this proxy.
- `.stream()`, `.astream()`, and a raw `stream=True` against OpenAI or
  Anthropic, read token by token, do not.

Read the call site before deciding. Report and stop only for an OpenAI or
Anthropic call read token by token; never silently disable streaming to make
it fit, and never report a Bedrock streaming call as a blocker.

## Credential behavior

- The proxy inherits the container's environment, so its upstream key is the
  port's own `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. There is no separate proxy
  process to keep the real key out of.
- Pointing `OPENAI_BASE_URL` at the proxy does not loop it back on itself: the
  proxy reads its upstream from `OPENAI_UPSTREAM_BASE` /
  `ANTHROPIC_UPSTREAM_BASE`, defaulting to the real providers.
- The OpenAI adapter removes caller authorization and inserts the proxy key.
- The Anthropic adapter removes caller key headers and inserts the proxy key.
- Botocore still signs requests sent to a custom endpoint, so a caller may need
  placeholder AWS credentials even though the proxy reissues upstream with its
  own identity.
- `/healthz` proves provider registration and Flask availability, not upstream
  credential validity.

OpenAI and Anthropic upstream HTTP errors pass through. Proxy exceptions return
JSON 502 with `error: proxy_error`. Bedrock `ClientError` bodies are reconstructed
with the upstream status and are not byte-for-byte passthrough.
