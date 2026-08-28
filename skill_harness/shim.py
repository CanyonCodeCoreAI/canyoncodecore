"""A pass-through proxy in front of the model providers.

Each repo is pointed at `/r/<slug>/<provider>/...` by its own `.env`, and the shim
forwards the request upstream unchanged except for the API key, which it swaps in
so the real credential never reaches the repo or its image.

It rewrites nothing else. Repos keep their own provider and their own model ids,
which is what M20 asks for; the shim exists for the two things a direct
connection cannot give — **per-repo token accounting**, which feeds the results
table, and one place that sees which models a repo actually calls.

Optional `rewrite` rules cover the case where a model id cannot be served as
written. Leave them out and the request passes through untouched.

Requests are buffered, not streamed, matching the scope llm_proxy already set.
When PR #54 lands this should fold into `llm_proxy/providers/`.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("shim")

# Per-repo token accounting, keyed by the slug in the request path.
USAGE: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
_USAGE_LOCK = threading.Lock()

# How each provider's wire protocol carries its credential. The upstream default
# is the provider's own API; pointing it elsewhere — a gateway, or Bedrock's
# compatible surfaces — is a config change rather than a code change.
PROTOCOLS = {
    "openai": {
        "upstream": "https://api.openai.com/v1",
        "auth_header": "Authorization",
        "auth_template": "Bearer {key}",
        "extra_headers": {},
    },
    "anthropic": {
        "upstream": "https://api.anthropic.com",
        "auth_header": "x-api-key",
        "auth_template": "{key}",
        "extra_headers": {"anthropic-version": "2023-06-01"},
    },
}

_PATH = re.compile(r"^/r/(?P<slug>[\w.\-]+)/(?P<provider>[\w\-]+)(?P<rest>/.*)$")

# Headers that describe the hop, not the request. Forwarding them corrupts the
# upstream call.
_HOP_BY_HOP = {"host", "content-length", "connection", "authorization", "x-api-key",
               "accept-encoding", "transfer-encoding"}


@dataclass
class Provider:
    """One open provider surface. A provider absent from the registry is closed."""

    name: str
    key: str
    upstream: str
    auth_header: str
    auth_template: str
    extra_headers: dict = field(default_factory=dict)
    # Optional model-id substitutions. Empty means pass through unchanged.
    rewrite_exact: dict = field(default_factory=dict)
    rewrite_prefixes: list = field(default_factory=list)
    _seen: dict = field(default_factory=dict)

    def resolve(self, model: str) -> str:
        target = self.rewrite_exact.get(model) or next(
            (dst for pre, dst in self.rewrite_prefixes if model.startswith(pre)), model
        )
        if self._seen.get(model) != target:
            self._seen[model] = target
            log.info("%s: %s%s", self.name, model,
                     "" if target == model else f" -> {target}")
        return target


def build_providers(config: dict, keys: dict[str, str]) -> dict[str, Provider]:
    """Assemble the open providers from config plus the keys actually present.

    A provider with no key is left out rather than half-configured: the screen
    reads the same registry, so a missing key becomes a stage 2 rejection instead
    of a failure at the first request, after an agent has been paid for.
    """
    out: dict[str, Provider] = {}
    for name, entry in (config or {}).items():
        proto = PROTOCOLS.get(name)
        if proto is None:
            log.warning("unknown provider %r in config; ignored", name)
            continue
        key = keys.get(name, "")
        if not key:
            log.info("provider %s has no key; that surface stays closed", name)
            continue
        rewrite = (entry or {}).get("rewrite") or {}
        out[name] = Provider(
            name=name,
            key=key,
            upstream=(entry or {}).get("upstream") or proto["upstream"],
            auth_header=proto["auth_header"],
            auth_template=proto["auth_template"],
            extra_headers=dict(proto["extra_headers"]),
            rewrite_exact=rewrite.get("exact") or {},
            rewrite_prefixes=[(r["prefix"], r["to"]) for r in rewrite.get("prefixes") or []],
        )
    return out


def _account(slug: str, raw: bytes) -> None:
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


def _handler_class(providers: dict[str, Provider], timeout: float):

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, code: int, detail: str):
            self._send(code, json.dumps({"error": "shim_error", "detail": detail}).encode())

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, json.dumps(
                    {"status": "ok", "providers": sorted(providers)}).encode())
            self._proxy(b"")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._proxy(self.rfile.read(length) if length else b"")

        def _proxy(self, body: bytes):
            match = _PATH.match(self.path)
            if not match:
                return self._fail(404, f"unroutable path {self.path!r}")
            slug, name, rest = match["slug"], match["provider"], match["rest"]

            provider = providers.get(name)
            if provider is None:
                # Worth a warning, not just a reply: a repo reaching a closed
                # provider means stage 2 let something through that it should
                # have rejected, and that is a screen defect to go and fix.
                log.warning("%s reached the closed %s surface", slug, name)
                return self._fail(503, f"the {name} surface is closed on this harness; "
                                       f"no key is configured for it")

            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and "model" in payload:
                    payload["model"] = provider.resolve(payload["model"])
                    body = json.dumps(payload).encode()

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP}
            headers[provider.auth_header] = provider.auth_template.format(key=provider.key)
            for k, v in provider.extra_headers.items():
                headers.setdefault(k, v)

            req = urllib.request.Request(
                f"{provider.upstream}{rest}", data=body or None,
                headers=headers, method=self.command,
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read()
            except Exception as e:  # network, DNS, timeout
                return self._fail(502, f"{type(e).__name__}: {e}")

            _account(slug, raw)
            self._send(status, raw)

    return Handler


def start(providers: dict[str, Provider], host: str = "0.0.0.0", port: int = 8300,
          timeout: float = 600.0) -> ThreadingHTTPServer:
    """Start the shim on a daemon thread and return the server.

    It binds 0.0.0.0 because the callers are agent containers, which reach the
    host by a different address than the harness does.
    """
    server = ThreadingHTTPServer((host, port), _handler_class(providers, timeout))
    threading.Thread(target=server.serve_forever, daemon=True, name="shim").start()
    log.info("shim on %s:%s -> %s", host, port,
             ", ".join(f"{n}={p.upstream}" for n, p in sorted(providers.items())) or "(nothing open)")
    return server


def usage_for(slug: str) -> dict[str, int]:
    with _USAGE_LOCK:
        return dict(USAGE[slug])
