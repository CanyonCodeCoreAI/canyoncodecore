"""The metrics seam.

Every proxied call passes through ``on_request`` / ``on_response``, which logs
and (when Redis is configured) extracts token usage per provider/op/model --
see ``Hooks._extract_usage``. Bedrock invoke's usage schema is only known for
anthropic.* models today; other model families and OpenAI/Anthropic streaming
remain unhandled.
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
                log.info(
                    "Redis telemetry enabled: %s:%s",
                    config.redis_host,
                    config.redis_port,
                )
            except Exception as e:
                log.warning("Redis not available: %s", e)

    def on_request(self, ctx: Ctx) -> None:
        log.info(
            "→ %s %s /%s model=%s (%d bytes)",
            ctx.provider,
            ctx.method,
            ctx.subpath,
            ctx.model,
            len(ctx.body),
        )

    def on_response(self, ctx: Ctx, resp: Any) -> None:
        usage = self._extract_usage(ctx, resp)
        is_stream = getattr(resp, "stream", None) is not None

        status = getattr(resp, "status", "?")
        if is_stream and getattr(resp, "stream_error", False):
            status = f"{status} (stream-error)"

        log.info(
            "← %s %s /%s -> %s in %.0fms | %s",
            ctx.provider,
            ctx.method,
            ctx.subpath,
            status,
            ctx.elapsed_ms(),
            usage or "no usage",
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

                    is_error = resp.status >= 400 or (
                        is_stream and getattr(resp, "stream_error", False)
                    )

                    # Build telemetry data
                    data = {
                        "model": model_id,
                        "errors": "1" if is_error else "0",
                    }

                    # Add token data if available
                    if usage:
                        data.update(
                            {
                                "input_token_count": str(usage.input_tokens),
                                "output_token_count": str(usage.output_tokens),
                                "token_count": str(usage.total_tokens),
                                "input_cache_tokens": str(usage.input_cache_tokens),
                                "input_cache_write_tokens": str(
                                    usage.input_cache_write_tokens
                                ),
                            }
                        )

                    self._redis.hset_multiple(f"future:{future_id}", data)
                    log.info(
                        "Wrote telemetry to future:%s with data: %s", future_id, data
                    )
                except Exception as e:
                    log.error("Failed to write telemetry: %s", e)

    def _extract_model_id(self, ctx: Ctx) -> str:
        """Extract model ID from context or subpath."""
        if ctx.model:
            return ctx.model
        if ctx.provider == "bedrock":
            model_id, _op = self._bedrock_model_and_op(ctx.subpath)
            if model_id:
                return model_id
        return "unknown"

    @staticmethod
    def _bedrock_model_and_op(subpath: str):
        """Split a Bedrock subpath ("model/<modelId>/<op>") into (model_id, op); (None, None) if unrecognized."""
        if not subpath.startswith("model/"):
            return None, None
        model_id, sep, op = subpath[len("model/") :].rpartition("/")
        return (model_id, op) if sep else (None, None)

    def _extract_usage(self, ctx: Ctx, resp: Any) -> Optional[TokenUsage]:
        """Dispatch to the right usage schema for this provider/op/model."""
        is_stream = getattr(resp, "stream", None) is not None

        if ctx.provider == "bedrock":
            if is_stream:
                return self._usage_from_dict(getattr(resp, "stream_usage", None))
            model_id, op = self._bedrock_model_and_op(ctx.subpath)
            # invoke's body is model-native; only anthropic.*'s schema is known so far.
            if op == "invoke" and (model_id or "").startswith("anthropic."):
                return self._extract_json_usage(resp, self._usage_from_anthropic_dict)
            return self._extract_json_usage(resp, self._usage_from_dict)

        if ctx.provider == "anthropic":
            return self._extract_json_usage(resp, self._usage_from_anthropic_dict)

        if ctx.provider == "openai":
            return self._extract_json_usage(resp, self._usage_from_openai_dict)

        return None

    @staticmethod
    def _extract_json_usage(resp: Any, parser) -> Optional[TokenUsage]:
        """Parse resp.content as JSON and hand its "usage" key to `parser`."""
        if getattr(resp, "status", None) != 200:
            return None
        try:
            data = json.loads(resp.content.decode("utf-8"))
        except Exception:
            return None
        return parser(data.get("usage"))

    @staticmethod
    def _usage_from_dict(usage: Optional[Dict[str, Any]]) -> Optional[TokenUsage]:
        """Bedrock Converse's usage schema (camelCase), used for both converse and converse-stream."""
        if not usage:
            return None
        return TokenUsage(
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            total_tokens=usage.get("totalTokens", 0),
            input_cache_tokens=usage.get("cacheReadInputTokens", 0),
            input_cache_write_tokens=usage.get("cacheCreationInputTokens", 0),
        )

    @staticmethod
    def _usage_from_anthropic_dict(
        usage: Optional[Dict[str, Any]],
    ) -> Optional[TokenUsage]:
        """Anthropic's native usage schema (snake_case, no total field); shared by direct Anthropic API calls and Bedrock invoke for anthropic.* models, since Bedrock returns Anthropic's own response body unchanged for that op."""
        if not usage:
            return None
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cache_tokens=usage.get("cache_read_input_tokens", 0),
            input_cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        )

    @staticmethod
    def _usage_from_openai_dict(
        usage: Optional[Dict[str, Any]],
    ) -> Optional[TokenUsage]:
        """OpenAI's native usage schema."""
        if not usage:
            return None
        return TokenUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )


hooks = Hooks()
