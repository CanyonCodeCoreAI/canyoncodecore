"""A model-id rewriting proxy in front of Bedrock.

Bedrock serves both the OpenAI Chat Completions wire format and the Anthropic
Messages wire format natively, so a ported repo needs no protocol translation —
only its base URL changed, which an environment variable can do. The one thing an
environment variable cannot reach is the model id, which travels in the request
body: a repo asks for `gpt-4o-mini` and Bedrock rejects it.

So this rewrites the `model` field and forwards everything else unchanged. That
is the whole job. See DESIGN.md section 3.

Requests are buffered, not streamed, matching the scope llm_proxy already set.
When PR #54 lands this should fold into `llm_proxy/providers/` as another
provider rather than continuing to live here.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("shim")

# Per-repo token accounting, keyed by the slug in the request path. This is the
# seam llm_proxy's hooks.py exists for; here it is four lines.
USAGE: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
_USAGE_LOCK = threading.Lock()

# Surface -> (upstream path prefix, auth header name, auth value template).
SURFACES = {
    "openai": ("/openai/v1", "Authorization", "Bearer {key}"),
    "anthropic": ("/anthropic", "x-api-key", "{key}"),
}

_PATH = re.compile(r"^/r/(?P<slug>[\w.\-]+)/(?P<surface>openai/v1|anthropic)(?P<rest>/.*)$")

# Headers that describe the hop, not the request. Forwarding them corrupts the
# upstream call.
_HOP_BY_HOP = {"host", "content-length", "connection", "authorization", "x-api-key",
               "accept-encoding", "transfer-encoding"}


class ModelMap:
    """Resolves a source model id to a Bedrock one.

    Exact matches first, then prefix rules, then a per-surface default. The
    resolved mapping is logged for every distinct source id so a result can
    always be read against the model that actually produced it.
    """

    def __init__(self, exact: dict[str, str], prefixes: list[tuple[str, str]],
                 defaults: dict[str, str]):
        self.exact = exact
        self.prefixes = prefixes
        self.defaults = defaults
        self.seen: dict[str, str] = {}

    def resolve(self, model: str, surface: str) -> str:
        if model in self.exact:
            target = self.exact[model]
        else:
            target = next(
                (dst for pre, dst in self.prefixes if model.startswith(pre)),
                self.defaults[surface],
            )
        if self.seen.get(model) != target:
            self.seen[model] = target
            log.info("model map: %s -> %s (%s)", model, target, surface)
        return target


def _handler_class(upstream_host: str, key: str, model_map: ModelMap, timeout: float):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter than the stdlib default
            log.debug(fmt, *args)

        def _fail(self, code: int, detail: str):
            body = json.dumps({"error": "shim_error", "detail": detail}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._proxy(b"")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._proxy(self.rfile.read(length) if length else b"")

        def _proxy(self, body: bytes):
            match = _PATH.match(self.path)
            if not match:
                return self._fail(404, f"unroutable path {self.path!r}")
            slug, surface_path, rest = match["slug"], match["surface"], match["rest"]
            surface = "openai" if surface_path.startswith("openai") else "anthropic"
            prefix, auth_header, auth_template = SURFACES[surface]

            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and "model" in payload:
                    payload["model"] = model_map.resolve(payload["model"], surface)
                    # Bedrock buffers; the repo may have asked to stream.
                    payload.pop("stream", None)
                    body = json.dumps(payload).encode()

            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            headers[auth_header] = auth_template.format(key=key)
            if surface == "anthropic":
                headers.setdefault("anthropic-version", "2023-06-01")

            req = urllib.request.Request(
                f"{upstream_host}{prefix}{rest}",
                data=body or None,
                headers=headers,
                method=self.command,
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
            except Exception as e:  # network, DNS, timeout
                return self._fail(502, f"{type(e).__name__}: {e}")

            self._account(slug, raw)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        @staticmethod
        def _account(slug: str, raw: bytes):
            try:
                usage = json.loads(raw).get("usage") or {}
            except (json.JSONDecodeError, AttributeError):
                return
            # OpenAI names them prompt/completion; Anthropic input/output.
            inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            with _USAGE_LOCK:
                bucket = USAGE[slug]
                bucket["input"] += inp
                bucket["output"] += out
                bucket["calls"] += 1

    return Handler


def start(region: str, key: str, model_map: ModelMap, host: str = "0.0.0.0",
          port: int = 8300, timeout: float = 600.0,
          upstream: str | None = None) -> ThreadingHTTPServer:
    """Start the shim on a daemon thread and return the server.

    It binds 0.0.0.0 because the callers are agent containers, which reach the
    host by a different address than the harness does. `upstream` is injectable
    so the shim can be tested without Bedrock, and pointed at `bedrock-mantle`
    without a code change.
    """
    upstream = upstream or f"https://bedrock-runtime.{region}.amazonaws.com"
    server = ThreadingHTTPServer(
        (host, port), _handler_class(upstream, key, model_map, timeout)
    )
    threading.Thread(target=server.serve_forever, daemon=True, name="shim").start()
    log.info("shim listening on %s:%s -> %s", host, port, upstream)
    return server


def usage_for(slug: str) -> dict[str, int]:
    with _USAGE_LOCK:
        return dict(USAGE[slug])
