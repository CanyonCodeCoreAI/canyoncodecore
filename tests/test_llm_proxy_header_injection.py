"""Unit tests for automatic future-id injection into httpx requests."""

import importlib.util
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import canyonos_core.controller.canyonos_context as canyonos_context
import canyonos_core.llm_proxy.proxy as proxy
from canyonos_core.llm_proxy.hooks import FUTURE_ID_HEADER


class HttpxHeaderInjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        canyonos_context._local = canyonos_context.threading.local()

    def tearDown(self):
        canyonos_context._local = canyonos_context.threading.local()

    def test_header_injected_for_openai_proxy_path(self):
        canyonos_context.set_current_future_id("future-sync")
        seen_headers = {}

        def handler(request):
            seen_headers.update(request.headers)
            return httpx.Response(200)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.get("http://proxy.test/openai/v1/chat/completions")

        self.assertEqual(seen_headers[FUTURE_ID_HEADER], "future-sync")

    async def test_header_injected_for_anthropic_proxy_path_async(self):
        canyonos_context.set_current_future_id("future-async")
        seen_headers = {}

        async def handler(request):
            seen_headers.update(request.headers)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await client.get("http://proxy.test/anthropic/v1/messages")

        self.assertEqual(seen_headers[FUTURE_ID_HEADER], "future-async")

    def test_header_not_injected_for_non_proxy_path(self):
        canyonos_context.set_current_future_id("future-direct")
        seen_headers = {}

        def handler(request):
            seen_headers.update(request.headers)
            return httpx.Response(200)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.get("https://api.anthropic.com/v1/messages")

        self.assertNotIn(FUTURE_ID_HEADER, seen_headers)

    def test_header_not_injected_without_future_id(self):
        seen_headers = {}

        def handler(request):
            seen_headers.update(request.headers)
            return httpx.Response(200)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.get("http://proxy.test/openai/v1/responses")

        self.assertNotIn(FUTURE_ID_HEADER, seen_headers)

    def test_second_module_import_does_not_double_patch(self):
        sync_send = httpx.Client.send
        async_send = httpx.AsyncClient.send
        spec = importlib.util.spec_from_file_location("flat_llm_proxy", proxy.__file__)
        duplicate = importlib.util.module_from_spec(spec)

        spec.loader.exec_module(duplicate)

        self.assertIs(httpx.Client.send, sync_send)
        self.assertIs(httpx.AsyncClient.send, async_send)


if __name__ == "__main__":
    unittest.main()
