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


def _stream_response(events, stream_usage=None):
    """Build a validly-framed event-stream ``ProxyResponse`` from a list of (event_type, body) pairs, shared by both Bedrock streaming stubs."""
    # Imported lazily so non-Bedrock stubs don't need boto3/botocore.
    from canyonos_core.llm_proxy.providers.bedrock import _event_frame

    def gen():
        for event_type, body in events:
            yield _event_frame(event_type, body)

    pr = ProxyResponse(
        status=200,
        headers=[("Content-Type", "application/vnd.amazon.eventstream")],
    )
    pr.stream = gen()
    pr.stream_usage = stream_usage
    return pr


def _bedrock_converse_stream_response(text):
    """A minimal ConverseStream event sequence so ``CANYONOS_LLM_STUB_TEXT``
    exercises the exact same wire format a real Bedrock call would, without
    needing AWS credentials."""
    usage = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}
    events = [
        ("messageStart", {"role": "assistant"}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}),
        ("contentBlockStop", {"contentBlockIndex": 0}),
        ("messageStop", {"stopReason": "end_turn"}),
        ("metadata", {"usage": usage}),
    ]
    return _stream_response(events, stream_usage=usage)


def _bedrock_invoke_stream_response(text):
    """A minimal InvokeModelWithResponseStream ``chunk`` event sequence; unlike
    ConverseStream there's no unified usage metadata event, so ``stream_usage``
    stays unset here (matches real Bedrock behavior for this op)."""
    chunk_body = json.dumps({"outputText": text, "generation": text}).encode("utf-8")
    events = [("chunk", {"bytes": chunk_body})]
    return _stream_response(events)


def build_stub(provider_name, subpath, text):
    """Build a provider-appropriate canned response carrying ``text``."""
    if provider_name == "bedrock":
        op = subpath.rsplit("/", 1)[-1] if subpath else ""
        if op == "converse-stream":
            return _bedrock_converse_stream_response(text)
        if op == "invoke-with-response-stream":
            return _bedrock_invoke_stream_response(text)
        if op == "converse":
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
