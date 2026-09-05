# LLM proxy integration

Read this only when the target checkout contains `llm_proxy` or the deployment
explicitly routes model SDKs through it.

## Preserve provider protocols

The proxy redirects provider endpoints; it does not convert providers. Keep the
source SDK, model ID, request body, and response parsing unchanged.

Configure only the provider variables the source uses:

```dotenv
OPENAI_BASE_URL=http://host.docker.internal:8081/openai/v1
ANTHROPIC_BASE_URL=http://host.docker.internal:8081/anthropic
AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://host.docker.internal:8081/bedrock
```

Some SDKs refuse to initialize without caller credentials. Give agent containers
non-secret placeholders only when required. Keep real OpenAI, Anthropic, or AWS
credentials in the separate proxy process, not in the port's `env_file`.

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
- OpenAI/Anthropic streaming is unsupported.
- Bedrock `invoke-with-response-stream`, `converse`, and `converse-stream` are
  unsupported.

Survey the source before selecting the proxy. Do not silently disable streaming;
report the unsupported behavior and stop.

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
