"""Configuration, read once from the environment at startup.

The proxy holds the *real* upstream credentials; callers can send dummy keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderConfig:
    upstream_base: str
    api_key: Optional[str] = None


@dataclass
class Config:
    host: str
    port: int
    connect_timeout: float
    read_timeout: float

    openai: ProviderConfig
    anthropic: ProviderConfig

    bedrock_region: str
    bedrock_upstream_host: str
    
    redis_host: str
    redis_port: int

    @classmethod
    def from_env(cls) -> "Config":
        region = (
            os.getenv("BEDROCK_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        return cls(
            host=os.getenv("PROXY_HOST", "127.0.0.1"),
            port=int(os.getenv("PROXY_PORT", "8080")),
            connect_timeout=float(os.getenv("PROXY_CONNECT_TIMEOUT", "10")),
            read_timeout=float(os.getenv("PROXY_READ_TIMEOUT", "600")),
            openai=ProviderConfig(
                upstream_base=os.getenv(
                    "OPENAI_UPSTREAM_BASE", "https://api.openai.com"
                ).rstrip("/"),
                api_key=os.getenv("OPENAI_API_KEY"),
            ),
            anthropic=ProviderConfig(
                upstream_base=os.getenv(
                    "ANTHROPIC_UPSTREAM_BASE", "https://api.anthropic.com"
                ).rstrip("/"),
                api_key=os.getenv("ANTHROPIC_API_KEY"),
            ),
            bedrock_region=region,
            bedrock_upstream_host=os.getenv(
                "BEDROCK_UPSTREAM_HOST", f"bedrock-runtime.{region}.amazonaws.com"
            ),
            redis_host=os.getenv("CANYONOS_REDIS_HOST", "localhost"),
            redis_port=int(os.getenv("CANYONOS_REDIS_PORT", "6379")),
        )
