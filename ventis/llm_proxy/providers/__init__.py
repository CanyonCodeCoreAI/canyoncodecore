from __future__ import annotations

from ventis.llm_proxy.providers.anthropic import AnthropicProvider
from ventis.llm_proxy.providers.bedrock import BedrockProvider
from ventis.llm_proxy.providers.openai import OpenAIProvider


def build_registry(cfg):
    """Map the URL prefix -> provider instance."""
    return {
        "openai": OpenAIProvider(cfg),
        "anthropic": AnthropicProvider(cfg),
        "bedrock": BedrockProvider(cfg),
    }
