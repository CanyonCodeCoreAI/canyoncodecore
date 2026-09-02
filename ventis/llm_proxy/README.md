# llm_proxy

A local, single-machine pass-through proxy for **OpenAI**, **Anthropic**, and
**Bedrock**. Callers keep their exact SDK calling convention — the only change is
one base-URL env var per provider. Every call flows through one function
(`core.proxy_request`) where token/metrics hooks fire.

**Scope:** request/response ("call and return") only. Streaming is intentionally
not implemented yet.

## How it works

```
your app (unchanged)         localhost:8080                 real upstream
  openai SDK   ─/openai/... ─┐
  anthropic SDK ─/anthropic/ ─┼─▶ proxy_request(ctx) ─▶ provider ─▶ api.openai.com
  boto3 bedrock ─/bedrock/... ┘    (metrics hooks)      adapter     api.anthropic.com
                                                                    bedrock-runtime.<region>.amazonaws.com
```

- **OpenAI / Anthropic** — straight HTTP reverse-proxy: rewrite host, swap in the
  real key, forward with `requests`, return the response.
- **Bedrock** — re-issued through the proxy's own `boto3` client (handles SigV4
  signing + URL-encoding correctly). Only `invoke` is wired up.

## Run

```bash
pip install -r llm_proxy/requirements.txt

# real upstream credentials live here; callers can use dummy keys
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
export AWS_REGION=us-east-1          # + normal AWS creds (env / ~/.aws / role)

python -m llm_proxy                  # listens on 127.0.0.1:8080
```

## Point your SDKs at it

No code changes — just env vars:

```bash
export OPENAI_BASE_URL=http://localhost:8080/openai/v1
export ANTHROPIC_BASE_URL=http://localhost:8080/anthropic
export AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://localhost:8080/bedrock
```

Then your existing code works unchanged:

```python
from openai import OpenAI
OpenAI().chat.completions.create(model="gpt-4o-mini",
                                 messages=[{"role": "user", "content": "hi"}])

from anthropic import Anthropic
Anthropic().messages.create(model="claude-3-5-sonnet-20241022", max_tokens=64,
                            messages=[{"role": "user", "content": "hi"}])

import boto3, json
boto3.client("bedrock-runtime").invoke_model(
    modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
    body=json.dumps({"anthropic_version": "bedrock-2023-05-31",
                     "max_tokens": 64,
                     "messages": [{"role": "user", "content": "hi"}]}))
```

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `PROXY_HOST` / `PROXY_PORT` | `127.0.0.1` / `8080` | where the proxy listens |
| `PROXY_CONNECT_TIMEOUT` / `PROXY_READ_TIMEOUT` | `10` / `600` | upstream timeouts (s) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | real upstream keys the proxy injects |
| `OPENAI_UPSTREAM_BASE` / `ANTHROPIC_UPSTREAM_BASE` | official APIs | override upstream (e.g. Azure/gateway) |
| `BEDROCK_REGION` (or `AWS_REGION`) | `us-east-1` | Bedrock region |
| `BEDROCK_UPSTREAM_HOST` | `bedrock-runtime.<region>.amazonaws.com` | override Bedrock host |

## Telemetry & Metrics

**Automatic telemetry is currently Bedrock-only.** The proxy captures:
- Model ID
- Input/output/total token counts
- Cache tokens (read & write)
- Error status

Telemetry is automatically written to Redis under `future:<future_id>` keys.

### How it works (Bedrock only)

1. **Auto-injection:** boto3 hook (`proxy.py`) injects `X-Ventis-Future-Id` header from thread-local context
2. **Token extraction:** `hooks.py` parses response `usage` field
3. **Redis write:** All metrics written to `future:<future_id>` hash

### Why Bedrock-only?

OpenAI and Anthropic use their own Python SDKs (`openai`, `anthropic`), not boto3.
The boto3 event hook doesn't fire for non-AWS SDKs. To add telemetry for those:
- Would need separate hooks in each SDK's HTTP client
- Or callers would need to use proxy directly (not through SDKs)

The proxy *forwards* OpenAI/Anthropic requests and *can* extract tokens, but doesn't
automatically inject headers or write telemetry.

## Limitations

- **No streaming.** `stream=True` / `invoke-with-response-stream` are not handled.
- **Bedrock error bodies are reconstructed**, not passed through byte-for-byte
  (boto3 raises on 4xx/5xx; we rebuild a JSON body with the real status +
  message). OpenAI/Anthropic errors pass through unchanged.
- **Dev server.** Runs on Flask's built-in server — fine for a local proxy, not
  meant for production traffic.
