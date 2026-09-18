"""Unit tests for streamed HTTP-provider passthrough and usage capture."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from canyonos_core.llm_proxy.hooks import Ctx, Hooks
from canyonos_core.llm_proxy.providers import base as base_module
from canyonos_core.llm_proxy.providers.anthropic import AnthropicProvider
from canyonos_core.llm_proxy.providers.openai import OpenAIProvider


class _ProviderCfg:
    upstream_base = "https://example.test"
    api_key = "key"


class _Cfg:
    connect_timeout = 1
    read_timeout = 2
    openai = _ProviderCfg()
    anthropic = _ProviderCfg()


class _Args:
    def to_dict(self, flat=True):
        return {}


class _Req:
    method = "POST"
    headers = {"content-type": "application/json"}
    args = _Args()


def _ctx(provider):
    return Ctx(
        provider=provider,
        method="POST",
        subpath="v1/messages",
        body=b"",
        headers={},
        t0=0.0,
    )


def _llm_stream_event(payload, event=None, newline=b"\n"):
    lines = []
    if event:
        lines.append(b"event: " + event.encode())
    lines.append(b"data: " + json.dumps(payload).encode())
    return newline.join(lines) + newline + newline


def _upstream(chunks, content_type="text/event-stream; charset=utf-8", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type, "X-Upstream": "value"}
    resp.content = b"buffered"
    resp.iter_content.return_value = iter(chunks)
    return resp


class HttpProviderStreamingTests(unittest.TestCase):
    def _forward(self, provider, upstream, body=b'{"stream": true}'):
        with patch.object(
            base_module.requests, "request", return_value=upstream
        ) as request:
            pr = provider.forward(_Req(), "v1/messages", body)
        self.assertTrue(request.call_args.kwargs["stream"])
        self.assertEqual(request.call_args.kwargs["data"], body)
        return pr

    def test_anthropic_stream_merges_usage_across_two_events(self):
        start = _llm_stream_event(
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 11,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 5,
                    }
                },
            },
            event="message_start",
        )
        delta = _llm_stream_event(
            {"type": "message_delta", "usage": {"output_tokens": 7}},
            event="message_delta",
        )
        chunks = [start[:17], start[17:] + delta[:9], delta[9:]]
        pr = self._forward(AnthropicProvider(_Cfg()), _upstream(chunks))

        self.assertEqual(list(pr.stream), chunks)
        usage = Hooks()._extract_usage(_ctx("anthropic"), pr)
        self.assertEqual(
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.input_cache_tokens,
                usage.input_cache_write_tokens,
            ),
            (11, 7, 18, 3, 5),
        )

    def test_openai_stream_captures_trailing_usage_chunk(self):
        chunks = [
            _llm_stream_event({"id": "c1", "choices": [{"delta": {"content": "hi"}}]}),
            _llm_stream_event(
                {
                    "id": "c1",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 8,
                        "total_tokens": 21,
                    },
                }
            ),
            b"data: [DONE]\n\n",
        ]
        pr = self._forward(OpenAIProvider(_Cfg()), _upstream(chunks))

        self.assertEqual(b"".join(pr.stream), b"".join(chunks))
        usage = Hooks()._extract_usage(_ctx("openai"), pr)
        self.assertEqual(
            (usage.input_tokens, usage.output_tokens, usage.total_tokens), (13, 8, 21)
        )

    def test_openai_stream_without_usage_chunk_reports_none(self):
        chunks = [
            _llm_stream_event({"id": "c1", "choices": [{"delta": {"content": "hi"}}]}),
            b"data: [DONE]\n\n",
        ]
        pr = self._forward(OpenAIProvider(_Cfg()), _upstream(chunks))

        self.assertEqual(b"".join(pr.stream), b"".join(chunks))
        self.assertIsNone(pr.stream_usage)
        self.assertIsNone(Hooks()._extract_usage(_ctx("openai"), pr))

    def test_unimplemented_provider_sets_stream_error_not_silent_zero(self):
        """A HttpProvider subclass that doesn't override merge_stream_usage must fail
        loudly (stream-error) rather than silently reporting no tokens forever."""

        class _NoUsageProvider(base_module.HttpProvider):
            def target(self, req, subpath, body):
                return base_module.UpstreamRequest(
                    "POST", "https://example.test/v1/messages", {}
                )

        chunks = [
            _llm_stream_event(
                {"type": "message_start", "message": {"usage": {"input_tokens": 4}}}
            )
        ]
        pr = self._forward(_NoUsageProvider(_Cfg()), _upstream(chunks))

        list(pr.stream)
        self.assertTrue(pr.stream_error)
        self.assertIsNone(pr.stream_usage)

    def test_non_stream_response_stays_buffered(self):
        pr = self._forward(
            AnthropicProvider(_Cfg()), _upstream([], content_type="application/json")
        )
        self.assertIsNone(pr.stream)
        self.assertEqual(pr.content, b"buffered")

    def test_mid_stream_failure_reaches_the_caller_instead_of_truncating(self):
        """An upstream that dies mid-stream must surface as an error to whoever is
        consuming the relay. Ending the generator quietly would frame a partial
        answer as a complete one, and the caller could not tell the difference."""
        delivered = _llm_stream_event(
            {"type": "message_start", "message": {"usage": {"input_tokens": 4}}}
        )

        def boom():
            yield delivered
            raise RuntimeError("upstream died")

        upstream = _upstream([])
        upstream.iter_content.return_value = boom()
        pr = self._forward(AnthropicProvider(_Cfg()), upstream)

        seen = []
        with self.assertRaises(RuntimeError):
            for chunk in pr.stream:
                seen.append(chunk)

        self.assertEqual(seen, [delivered])
        self.assertTrue(pr.stream_error)
        self.assertEqual(Hooks()._extract_usage(_ctx("anthropic"), pr).input_tokens, 4)
        upstream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
