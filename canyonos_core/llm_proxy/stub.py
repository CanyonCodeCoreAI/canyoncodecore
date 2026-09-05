"""Test-mode LLM stub.

When ``CANYONOS_LLM_STUB_TEXT`` is set in the environment, every proxied LLM
call short-circuits and returns that text as the model output instead of hitting
a real upstream (Bedrock/OpenAI/Anthropic). This lets a full workflow be
exercised end-to-end with no cloud credentials and zero token cost -- the whole
deploy/route/proxy/telemetry path still runs, only the upstream call is replaced.

Enable it per-deploy via an ``env_file`` entry (injected into every agent
container, and inherited by the in-container proxy subprocess):

    # .env
    CANYONOS_LLM_STUB_TEXT=testing
"""

from __future__ import annotations

import json
import os

from canyonos_core.llm_proxy.providers.base import ProxyResponse

STUB_ENV = "CANYONOS_LLM_STUB_TEXT"


def stub_text():
    """Return the configured stub text, or None when stubbing is disabled."""
    return os.getenv(STUB_ENV)


def _json_response(obj, status=200):
    return ProxyResponse(
        status=status,
        headers=[("Content-Type", "application/json")],
        content=json.dumps(obj).encode("utf-8"),
    )


def build_stub(provider_name, subpath, text):
    """Build a provider-appropriate canned response carrying ``text``."""
    if provider_name == "bedrock":
        op = subpath.rsplit("/", 1)[-1] if subpath else ""
        if op in ("converse", "converse-stream"):
            return _json_response({
                "output": {"message": {"role": "assistant",
                                       "content": [{"text": text}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            })
        # invoke / other ops: a minimal body that common model families read.
        return _json_response({
            "outputText": text,
            "results": [{"outputText": text}],
            "generation": text,
        })

    if provider_name == "openai":
        return _json_response({
            "id": "stub-cmpl", "object": "chat.completion", "model": "stub",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    if provider_name == "anthropic":
        return _json_response({
            "id": "stub-msg", "type": "message", "role": "assistant", "model": "stub",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    return _json_response({"text": text})
