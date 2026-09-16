"""Single source of truth for the env vars that route an agent's LLM SDK calls
through the in-container LLM proxy (started by LocalController; see
canyonos_core/llm_proxy/).

Every LLM SDK/library reads a different env var name for its base URL -- there
is no universal name to standardize on, so all of them still have to be set.
What this module collapses is the VALUE: one PROXY_HOST constant, one place
that lists the var names, used by both the Local and EC2 runtime backends
instead of two independently hand-maintained copies of the same six lines.

See company-memory: "LLM proxy can infer the provider from the request; the
six base-URL variable names still cannot be collapsed" (CAN-343 analysis).
"""

# The proxy always runs on localhost inside the agent's own container.
PROXY_HOST = "http://127.0.0.1:8081"


def llm_proxy_env_vars(proxy_host: str = PROXY_HOST) -> dict:
    """Return the {env_var_name: value} pairs that route Bedrock/OpenAI/Anthropic
    SDK traffic through the in-container LLM proxy at `proxy_host`.
    """
    return {
        # boto3 (Bedrock)
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": f"{proxy_host}/bedrock",
        # openai SDK
        "OPENAI_BASE_URL": f"{proxy_host}/openai/v1",
        # langchain_openai / llama_index
        "OPENAI_API_BASE": f"{proxy_host}/openai/v1",
        # anthropic SDK
        "ANTHROPIC_BASE_URL": f"{proxy_host}/anthropic",
        # langchain_anthropic
        "ANTHROPIC_API_URL": f"{proxy_host}/anthropic",
        # LiteLLM
        "ANTHROPIC_API_BASE": f"{proxy_host}/anthropic",
    }


def llm_proxy_docker_env_args(proxy_host: str = PROXY_HOST) -> list:
    """Return `llm_proxy_env_vars` flattened into repeated `-e NAME=VALUE` pairs,
    ready to splice into a `docker run` argument list.
    """
    args = []
    for name, value in llm_proxy_env_vars(proxy_host).items():
        args.append("-e")
        args.append(f"{name}={value}")
    return args
