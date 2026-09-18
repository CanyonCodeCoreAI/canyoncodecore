"""Provider abstraction and shared HTTP plumbing.

A provider's only job is to take the incoming request and produce a
``ProxyResponse``. Straight HTTP reverse-proxy providers (OpenAI, Anthropic)
subclass ``HttpProvider`` and just describe the upstream target; Bedrock owns
its own ``forward`` because it re-issues through boto3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# Request headers we never forward: hop-by-hop (RFC 7230), ones we rewrite, and
# accept-encoding (we let the HTTP client negotiate + decode, then re-frame the
# response ourselves).
DROP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

# Response headers we drop: we return already-decoded content and let the WSGI
# layer recompute framing headers.
DROP_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
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
    content: bytes = b""
    # Set instead of ``content`` for streamed responses; core.proxy_request streams these chunks through directly.
    stream: Optional[Iterator[bytes]] = None
    # Filled in by the stream generator once usage is known (only available after the trailing "metadata" event).
    stream_usage: Optional[dict] = None
    stream_error: bool = False

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
    return [
        (k, v) for k, v in headers.items() if k.lower() not in DROP_RESPONSE_HEADERS
    ]


class Provider:
    name = "base"

    def __init__(self, cfg):
        self.cfg = cfg

    def forward(self, req, subpath: str, body: bytes) -> ProxyResponse:
        raise NotImplementedError


def parse_llm_stream_line(line: bytes) -> Optional[dict]:
    """Decode one streamed LLM response "data:" line into a dict, or None if it carries no JSON."""
    if not line.startswith(b"data:"):
        return None
    data = line[len(b"data:") :].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


class HttpProvider(Provider):
    """Providers that are a straight HTTP reverse-proxy (OpenAI, Anthropic)."""

    def target(self, req, subpath: str, body: bytes) -> UpstreamRequest:
        raise NotImplementedError

    def merge_stream_usage(self, payload: dict, usage: Dict[str, Any]) -> None:
        """
        Anthropic and OpenAI get their own versions of this, so if this gets called a non-supported provider got called, and should error as a result
        """
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
            stream=True,
        )
        headers = filter_response_headers(resp.headers)
        # Media types are case-insensitive and may carry parameters, so compare the
        # type alone rather than substring-matching the raw header.
        content_type = resp.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
            return ProxyResponse(
                status=resp.status_code, headers=headers, content=resp.content
            )
        pr = ProxyResponse(status=resp.status_code, headers=headers)
        pr.stream = self._relay_llm_stream(resp, pr)
        return pr

    def _relay_llm_stream(self, resp, pr: ProxyResponse):
        """Relay streamed LLM response bytes untouched while folding usage out of the events in passing."""
        usage: Dict[str, Any] = {}
        buf = b""
        completed = False
        try:
            for chunk in resp.iter_content(chunk_size=None):
                yield chunk
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for line in lines:
                    try:
                        payload = parse_llm_stream_line(line)
                        if isinstance(payload, dict):
                            self.merge_stream_usage(payload, usage)
                    except Exception:  # noqa: BLE001 - telemetry must never truncate the relay
                        log.warning(
                            "Failed to parse stream usage for telemetry", exc_info=True
                        )
                        pr.stream_error = True
            completed = True
        except Exception:
            # Swallowing this would hand the caller a partial answer framed as a
            # complete one: HTTP 200 with a clean end-of-stream. Re-raise so the
            # WSGI layer abandons the response without its terminating chunk and
            # the client's HTTP library errors, as a direct provider call would.
            log.warning("LLM upstream stream ended early", exc_info=True)
            pr.stream_error = True
            raise
        finally:
            resp.close()
            # Publish usage only when this relay owns how the stream ended: a normal
            # finish, or an upstream failure, which stream_error marks so a partial
            # count is not read as a complete one. A caller that disconnects mid-
            # stream leaves the final usage event unread, and publishing what arrived
            # so far would record a short count that looks like a finished, cheaper
            # call than actually happened.
            if usage and (completed or pr.stream_error):
                pr.stream_usage = usage
