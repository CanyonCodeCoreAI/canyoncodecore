"""Provider abstraction and shared HTTP plumbing.

A provider's only job is to take the incoming request and produce a
``ProxyResponse``. Straight HTTP reverse-proxy providers (OpenAI, Anthropic)
subclass ``HttpProvider`` and just describe the upstream target; Bedrock owns
its own ``forward`` because it re-issues through boto3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

import requests

# Request headers we never forward: hop-by-hop (RFC 7230), ones we rewrite, and
# accept-encoding (we let the HTTP client negotiate + decode, then re-frame the
# response ourselves).
DROP_REQUEST_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}

# Response headers we drop: we return already-decoded content and let the WSGI
# layer recompute framing headers.
DROP_RESPONSE_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding",
    "connection", "keep-alive",
}


@dataclass
class UpstreamRequest:
    method: str
    url: str
    headers: Dict[str, str]
    params: Dict[str, str] = field(default_factory=dict)


@dataclass
class ProxyResponse:
    status: int
    headers: List[Tuple[str, str]]
    content: bytes

    def json(self):
        return json.loads(self.content.decode("utf-8"))


def client_headers(incoming, drop: Iterable[str] = ()) -> Dict[str, str]:
    """Copy the caller's headers minus the ones we must not forward."""
    extra = {d.lower() for d in drop}
    return {
        k: v
        for k, v in incoming.headers.items()
        if k.lower() not in DROP_REQUEST_HEADERS and k.lower() not in extra
    }


def filter_response_headers(headers) -> List[Tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in DROP_RESPONSE_HEADERS]


class Provider:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def forward(self, req, subpath: str, body: bytes) -> ProxyResponse:
        raise NotImplementedError


class HttpProvider(Provider):
    """Providers that are a straight HTTP reverse-proxy (OpenAI, Anthropic)."""

    def target(self, req, subpath: str, body: bytes) -> UpstreamRequest:
        raise NotImplementedError

    def forward(self, req, subpath, body):
        up = self.target(req, subpath, body)
        resp = requests.request(
            up.method,
            up.url,
            headers=up.headers,
            params=up.params,
            data=body,
            timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
        )
        return ProxyResponse(
            status=resp.status_code,
            headers=filter_response_headers(resp.headers),
            content=resp.content,
        )
