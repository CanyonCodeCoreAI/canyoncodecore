from __future__ import annotations

from canyonos_core.llm_proxy.providers.anthropic import AnthropicProvider
from canyonos_core.llm_proxy.providers.bedrock import BedrockProvider
from canyonos_core.llm_proxy.providers.openai import OpenAIProvider


def build_registry(cfg):
    """Map the URL prefix -> provider instance."""
    return {
        "openai": OpenAIProvider(cfg),
        "anthropic": AnthropicProvider(cfg),
        "bedrock": BedrockProvider(cfg),
    }
