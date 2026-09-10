"""End-to-end: Flask test client -> core.proxy_request -> stub converse-stream
response, decoded the same way a real boto3 caller would."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from botocore.eventstream import EventStreamBuffer

from canyonos_core.llm_proxy.app import create_app
from canyonos_core.llm_proxy.config import Config, ProviderConfig


def _decode_frames(raw: bytes):
    buf = EventStreamBuffer()
    buf.add_data(raw)
    return [(msg.headers, msg.payload) for msg in buf]


class ConverseStreamStubE2ETests(unittest.TestCase):
    def setUp(self):
        cfg = Config(
            host="127.0.0.1", port=0, connect_timeout=1, read_timeout=1,
            openai=ProviderConfig(upstream_base="https://api.openai.com"),
            anthropic=ProviderConfig(upstream_base="https://api.anthropic.com"),
            bedrock_region="us-east-1",
            bedrock_upstream_host="bedrock-runtime.us-east-1.amazonaws.com",
            redis_host="localhost", redis_port=6379,
        )
        self.app = create_app(cfg)
        self.client = self.app.test_client()

    @patch.dict(os.environ, {"CANYONOS_LLM_STUB_TEXT": "hello from stub"})
    def test_converse_stream_returns_valid_eventstream_body(self):
        resp = self.client.post(
            "/bedrock/model/anthropic.claude-3-5-sonnet-20240620-v1:0/converse-stream",
            data=b'{"messages": [{"role": "user", "content": [{"text": "hi"}]}]}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"], "application/vnd.amazon.eventstream")

        decoded = _decode_frames(resp.data)
        event_types = [h[":event-type"] for h, _ in decoded]
        self.assertEqual(
            event_types,
            ["messageStart", "contentBlockDelta", "contentBlockStop",
             "messageStop", "metadata"],
        )

        import json
        delta_payload = json.loads(decoded[1][1])
        self.assertEqual(delta_payload["delta"]["text"], "hello from stub")

    @patch.dict(os.environ, {"CANYONOS_LLM_STUB_TEXT": "hello from stub"})
    def test_invoke_with_response_stream_returns_valid_eventstream_body(self):
        resp = self.client.post(
            "/bedrock/model/meta.llama3-8b-instruct-v1:0/invoke-with-response-stream",
            data=b'{"prompt": "hi"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Content-Type"], "application/vnd.amazon.eventstream")

        decoded = _decode_frames(resp.data)
        self.assertEqual([h[":event-type"] for h, _ in decoded], ["chunk"])

        import base64
        import json
        chunk_payload = json.loads(decoded[0][1])
        chunk_bytes = base64.b64decode(chunk_payload["bytes"])
        self.assertEqual(json.loads(chunk_bytes)["generation"], "hello from stub")


if __name__ == "__main__":
    unittest.main()
