# LLM proxy integration

Read this only when the target checkout contains `llm_proxy` or the deployment
explicitly routes model SDKs through it.

## Contents

- Preserve provider protocols
- Set every spelling, not the one you expect
- A source with no env hook cannot be proxied
- Start locally
- Supported call shape
- Credential behavior

## Preserve provider protocols

The proxy redirects provider endpoints; it does not convert providers. Keep the
source SDK, model ID, request body, and response parsing unchanged.

## Set every spelling, not the one you expect

Each SDK generation reads a different base-URL variable, and a wrapper library
reads a different one from the SDK it wraps. Set only the name this reference
used to give and the container reaches the real provider with a placeholder key:
a 401 that reads like a broken port, after validation and the deployment build
have passed. Set all of them for whichever providers the source uses:

```dotenv
OPENAI_BASE_URL=http://host.docker.internal:8081/openai/v1
OPENAI_API_BASE=http://host.docker.internal:8081/openai/v1
ANTHROPIC_BASE_URL=http://host.docker.internal:8081/anthropic
ANTHROPIC_API_URL=http://host.docker.internal:8081/anthropic
ANTHROPIC_API_BASE=http://host.docker.internal:8081/anthropic
AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://host.docker.internal:8081/bedrock
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

Confirm the route rather than assuming it: the proxy logs one line per forwarded
call, so an empty proxy log after a successful request means the container went
straight to the provider.

Some SDKs refuse to initialize without caller credentials. Give agent containers
non-secret placeholders only when required. Keep real OpenAI, Anthropic, or AWS
credentials in the separate proxy process, not in the port's `env_file`.

## A source with no env hook cannot be proxied

Some sources build the HTTP call themselves -- `urllib.request` against a module
constant like `API = "https://api.openai.com/v1/responses"` -- and read no
base-URL variable at all. Editing that constant is a source edit M18 and M21
forbid, so the `env_file` is inert and the container can only ever reach the
real provider. Report this as a proxy blocker and stop. Do not hand the
container a real upstream credential instead.

Detect it before deploying: grep the source for the provider hostname. A literal
`api.openai.com` or `api.anthropic.com` outside a comment means the call bypasses
the SDK's base-URL resolution entirely.

## Start locally

The proxy defaults conflict with a typical deployment: host loopback is not
reachable from a container, and port 8080 is normally used by the workflow API.
Use a non-loopback bind and a different port:

```bash
PROXY_HOST=0.0.0.0 PROXY_PORT=8081 python -m llm_proxy
curl http://127.0.0.1:8081/healthz
```

Local CanyonOS Core containers resolve `host.docker.internal` through their
Docker host mapping. On EC2 that name resolves to each EC2 Docker host, not the
machine running `canyonos deploy`. Distributed deployments need a reachable proxy
address or one proxy on each host.

## Supported call shape

The implementation buffers complete requests and responses:

- OpenAI and Anthropic non-streaming HTTP calls are forwarded.
- Bedrock `invoke` is reissued through the proxy's boto3 client.
- Bedrock `invoke-with-response-stream`, `converse`, and `converse-stream` are
  unsupported.

Streaming splits by how the source **consumes** the response, not by whether a
`streaming` flag is set. The proxy buffers, so it forwards anything that reads a
complete response and breaks anything that reads tokens as they arrive:

- `ChatOpenAI(streaming=True)` reached through `.invoke()` works. LangChain
  drains the stream inside the call and returns one message; the proxy sees an
  ordinary buffered request. Verified end to end against this proxy.
- `.stream()`, `.astream()`, and a raw `stream=True` read token by token do not.

Read the call site before deciding. Report and stop only for the second kind;
never silently disable streaming to make the first kind fit.

## Credential behavior

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
