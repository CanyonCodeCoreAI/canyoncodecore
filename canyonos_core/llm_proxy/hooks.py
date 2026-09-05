"""The metrics seam.

Every proxied call passes through ``on_request`` / ``on_response``. Today these
only log. Token accounting lands here later: because the whole response is
buffered, usage extraction is a one-liner, e.g. ``resp.json().get("usage")`` for
OpenAI/Anthropic (Bedrock's usage lives in its per-model response body).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger("llm_proxy")


@dataclass
class TokenUsage:
    """Token usage extracted from LLM responses."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cache_tokens: int = 0
    input_cache_write_tokens: int = 0
    
    def __repr__(self):
        parts = [f"in={self.input_tokens}", f"out={self.output_tokens}"]
        if self.input_cache_tokens:
            parts.append(f"cache_read={self.input_cache_tokens}")
        if self.input_cache_write_tokens:
            parts.append(f"cache_write={self.input_cache_write_tokens}")
        return f"TokenUsage({', '.join(parts)})"


@dataclass
class Ctx:
    provider: str
    method: str
    subpath: str
    body: bytes
    headers: Dict[str, str]
    t0: float
    model: Optional[str] = None

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0


class Hooks:
    def __init__(self, config=None):
        self.config = config
        self._redis = None
        
        if config:
            try:
                try:
                    from canyonos_core.controller.utils.redis_client import RedisClient
                except ImportError:
                    # In-container the framework files are copied flat to /app.
                    from redis_client import RedisClient
                self._redis = RedisClient(
                    host=config.redis_host,
                    port=config.redis_port,
                )
                log.info("Redis telemetry enabled: %s:%s", config.redis_host, config.redis_port)
            except Exception as e:
                log.warning("Redis not available: %s", e)
    
    def on_request(self, ctx: Ctx) -> None:
        log.info(
            "→ %s %s /%s model=%s (%d bytes)",
            ctx.provider, ctx.method, ctx.subpath, ctx.model, len(ctx.body),
        )

    def on_response(self, ctx: Ctx, resp: Any) -> None:
        # Extract tokens for Bedrock
        usage = None
        if ctx.provider == "bedrock":
            usage = self._extract_bedrock_tokens(resp)
        
        log.info(
            "← %s %s /%s -> %s in %.0fms | %s",
            ctx.provider, ctx.method, ctx.subpath,
            getattr(resp, "status", "?"), ctx.elapsed_ms(),
            usage or "no usage"
        )
        
        # Write to Redis if we have context
        log.info("Checking telemetry write: redis=%s", "yes" if self._redis else "no")
        if self._redis:
            future_id = ctx.headers.get("X-Canyonos-Future-ID")
            log.info("Future ID from headers: %s", future_id)
            if future_id:
                try:
                    # Extract model ID
                    model_id = self._extract_model_id(ctx)
                    
                    is_error = resp.status >= 400
                    
                    # Build telemetry data
                    data = {
                        "model": model_id,
                        "errors": "1" if is_error else "0",
                    }
                    
                    # Add token data if available
                    if usage:
                        data.update({
                            "input_token_count": str(usage.input_tokens),
                            "output_token_count": str(usage.output_tokens),
                            "token_count": str(usage.total_tokens),
                            "input_cache_tokens": str(usage.input_cache_tokens),
                            "input_cache_write_tokens": str(usage.input_cache_write_tokens),
                        })
                    
                    self._redis.hset_multiple(f"future:{future_id}", data)
                    log.info("Wrote telemetry to future:%s with data: %s", future_id, data)
                except Exception as e:
                    log.error("Failed to write telemetry: %s", e)
    
    def _extract_model_id(self, ctx: Ctx) -> str:
        """Extract model ID from context or subpath."""
        if ctx.model:
            return ctx.model
        
        # For Bedrock: subpath is "model/<modelId>/operation"
        # Use rpartition to peel operation off the right (same as provider logic)
        if ctx.provider == "bedrock" and ctx.subpath.startswith("model/"):
            model_id, sep, op = ctx.subpath[len("model/"):].rpartition("/")
            if sep:  # Found a separator
                return model_id
        
        return "unknown"
    
    def _extract_bedrock_tokens(self, resp: Any) -> Optional[TokenUsage]:
        """Extract token usage from Bedrock response. It requires diff logic from OpenAI/Anthropic"""
        if resp.status != 200:
            return None
        
        try:
            data = json.loads(resp.content.decode("utf-8"))
            usage = data.get("usage", {})
            if usage:
                return TokenUsage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                    total_tokens=usage.get("totalTokens", 0),
                    input_cache_tokens=usage.get("cacheReadInputTokens", 0),
                    input_cache_write_tokens=usage.get("cacheCreationInputTokens", 0),
                )
        except:
            pass
        return None


hooks = Hooks()
