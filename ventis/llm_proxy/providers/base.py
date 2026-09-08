"""Provider abstraction and shared HTTP plumbing.

A provider's only job is to take the incoming request and produce a
``ProxyResponse``. Straight HTTP reverse-proxy providers (OpenAI, Anthropic)
subclass ``HttpProvider`` and just describe the upstream target; Bedrock owns
its own ``forward`` because it re-issues through boto3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

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

    def check_model(self, model_id: str) -> dict:
        """Verify a specific model is reachable with this provider's credentials.

        Used by ``/healthz?<provider>_model=<id>``. Returns a JSON-serializable
        dict with at least ``model`` and ``ok``.
        """
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

    # -- model existence/access check, shared by OpenAI + Anthropic -------
    #
    # Both providers expose a free `GET /v1/models/{id}` that succeeds only if
    # the credential is valid *and* the model exists/is accessible to the
    # account, so a real request is cheap enough to make on every check.

    def _model_url(self, model_id: str) -> str:
        raise NotImplementedError

    def _model_check_headers(self) -> Optional[Dict[str, str]]:
        """Auth headers for the model check, or None if no key is configured."""
        raise NotImplementedError

    def check_model(self, model_id: str) -> dict:
        headers = self._model_check_headers()
        if headers is None:
            return {"model": model_id, "ok": False, "error": "no API key configured"}
        try:
            resp = requests.get(
                self._model_url(model_id),
                headers=headers,
                timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
            )
        except requests.RequestException as exc:
            return {"model": model_id, "ok": False, "error": str(exc)}
        if resp.status_code == 200:
            return {"model": model_id, "ok": True}
        return {"model": model_id, "ok": False, "error": f"upstream returned {resp.status_code}"}
