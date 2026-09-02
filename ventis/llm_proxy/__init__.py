"""Local LLM proxy.

A transparent, single-machine pass-through for OpenAI, Anthropic, and Bedrock.
Point each provider's SDK at this service via its base-URL env var and calls flow
through one choke point (``llm_proxy.core.proxy_request``) where request/response
metrics hooks fire.

Scope: request/response ("call and return") only. Streaming is intentionally
not implemented yet.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
