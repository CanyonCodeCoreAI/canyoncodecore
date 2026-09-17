import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from botocore.eventstream import EventStreamBuffer

from canyonos_core.llm_proxy.providers import bedrock as bedrock_module
from canyonos_core.llm_proxy.providers.bedrock import (
    BedrockProvider,
    _encode_event,
    _event_frame,
    _exception_frame,
)


def _decode_frames(raw: bytes):
    """Feed raw bytes through botocore's real decoder and return the parsed
    (headers, payload) pairs -- proves our encoder is wire-compatible with
    the same parser a caller's boto3 client would use."""
    buf = EventStreamBuffer()
    buf.add_data(raw)
    return [(msg.headers, msg.payload) for msg in buf]


class EncodeEventTests(unittest.TestCase):
    def test_round_trips_through_botocore_decoder(self):
        headers = {
            ":event-type": "contentBlockDelta",
            ":content-type": "application/json",
            ":message-type": "event",
        }
        payload = json.dumps({"delta": {"text": "hi"}}).encode("utf-8")
        raw = _encode_event(headers, payload)

        [(decoded_headers, decoded_payload)] = _decode_frames(raw)
        self.assertEqual(decoded_headers, headers)
        self.assertEqual(decoded_payload, payload)

    def test_event_frame_helper(self):
        raw = _event_frame("messageStart", {"role": "assistant"})
        [(headers, payload)] = _decode_frames(raw)
        self.assertEqual(headers[":event-type"], "messageStart")
        self.assertEqual(headers[":message-type"], "event")
        self.assertEqual(json.loads(payload), {"role": "assistant"})

    def test_exception_frame_helper(self):
        raw = _exception_frame("ThrottlingException", "slow down")
        [(headers, payload)] = _decode_frames(raw)
        self.assertEqual(headers[":error-code"], "ThrottlingException")
        self.assertEqual(headers[":error-message"], "slow down")
        self.assertEqual(headers[":message-type"], "exception")
        self.assertEqual(json.loads(payload), {"message": "slow down"})

    def test_multiple_events_concatenate_and_decode_in_order(self):
        raw = (
            _event_frame("messageStart", {"role": "assistant"})
            + _event_frame("contentBlockDelta", {"delta": {"text": "hi"}})
            + _event_frame("messageStop", {"stopReason": "end_turn"})
        )
        decoded = _decode_frames(raw)
        self.assertEqual([h[":event-type"] for h, _ in decoded],
                         ["messageStart", "contentBlockDelta", "messageStop"])


class _FakeCfg:
    bedrock_region = "us-east-1"
    bedrock_upstream_host = "bedrock-runtime.us-east-1.amazonaws.com"


class BedrockProviderConverseStreamTests(unittest.TestCase):
    def _make_provider(self):
        with patch.object(bedrock_module.boto3, "client") as mock_client_factory:
            self.mock_client = MagicMock()
            mock_client_factory.return_value = self.mock_client
            return BedrockProvider(_FakeCfg())

    def test_converse_stream_encodes_events_and_captures_usage(self):
        provider = self._make_provider()
        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 3, "outputTokens": 5,
                                     "totalTokens": 8}}},
        ]
        self.mock_client.converse_stream.return_value = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "stream": iter(events),
        }

        req = MagicMock()
        body = json.dumps({"messages": [{"role": "user", "content": [{"text": "hi"}]}]}).encode()
        pr = provider.forward(req, "model/anthropic.claude-3/converse-stream", body)

        self.assertIsNotNone(pr.stream)
        self.assertEqual(pr.headers, [("Content-Type", "application/vnd.amazon.eventstream")])

        raw = b"".join(pr.stream)
        decoded = _decode_frames(raw)
        self.assertEqual(
            [h[":event-type"] for h, _ in decoded],
            ["messageStart", "contentBlockDelta", "messageStop", "metadata"],
        )
        self.assertEqual(json.loads(decoded[1][1]), {"delta": {"text": "hi"}})

        # Usage is only populated once the generator has actually been drained.
        self.assertEqual(pr.stream_usage, {"inputTokens": 3, "outputTokens": 5,
                                            "totalTokens": 8})
        self.assertFalse(pr.stream_error)

        self.mock_client.converse_stream.assert_called_once()
        called_kwargs = self.mock_client.converse_stream.call_args.kwargs
        self.assertEqual(called_kwargs["modelId"], "anthropic.claude-3")

    def test_mid_stream_error_yields_exception_frame_instead_of_raising(self):
        provider = self._make_provider()

        def failing_events():
            yield {"messageStart": {"role": "assistant"}}
            raise RuntimeError("boom")

        self.mock_client.converse_stream.return_value = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "stream": failing_events(),
        }

        req = MagicMock()
        body = json.dumps({"messages": []}).encode()
        pr = provider.forward(req, "model/anthropic.claude-3/converse-stream", body)

        raw = b"".join(pr.stream)  # must not raise
        decoded = _decode_frames(raw)
        self.assertEqual(decoded[0][0][":event-type"], "messageStart")
        self.assertEqual(decoded[1][0][":message-type"], "exception")
        self.assertTrue(pr.stream_error)


class BedrockProviderInvokeStreamTests(unittest.TestCase):
    def _make_provider(self):
        with patch.object(bedrock_module.boto3, "client") as mock_client_factory:
            self.mock_client = MagicMock()
            mock_client_factory.return_value = self.mock_client
            return BedrockProvider(_FakeCfg())

    def test_invoke_stream_encodes_chunks(self):
        provider = self._make_provider()
        chunk_bytes = [
            json.dumps({"generation": "hi"}).encode("utf-8"),
            json.dumps({"generation": " there"}).encode("utf-8"),
        ]
        events = [{"chunk": {"bytes": b}} for b in chunk_bytes]
        self.mock_client.invoke_model_with_response_stream.return_value = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "body": iter(events),
        }

        req = MagicMock()
        req.headers = {}
        body = json.dumps({"prompt": "hi"}).encode()
        pr = provider.forward(req, "model/meta.llama3-8b/invoke-with-response-stream", body)

        self.assertIsNotNone(pr.stream)
        raw = b"".join(pr.stream)
        decoded = _decode_frames(raw)
        self.assertEqual([h[":event-type"] for h, _ in decoded], ["chunk", "chunk"])

        import base64
        first_payload = json.loads(decoded[0][1])
        self.assertEqual(base64.b64decode(first_payload["bytes"]), chunk_bytes[0])
        self.assertIsNone(pr.stream_usage)  # no usage metadata event for this op
        self.assertFalse(pr.stream_error)

        self.mock_client.invoke_model_with_response_stream.assert_called_once()
        called_kwargs = self.mock_client.invoke_model_with_response_stream.call_args.kwargs
        self.assertEqual(called_kwargs["modelId"], "meta.llama3-8b")


if __name__ == "__main__":
    unittest.main()
