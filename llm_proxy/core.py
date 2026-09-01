"""The single choke point every proxied call flows through."""

from __future__ import annotations

import json
import time
from typing import Optional

from flask import Response

from llm_proxy.hooks import Ctx, hooks


def _guess_model(body: bytes) -> Optional[str]:
    """Best-effort model name from the JSON body, for logging/metrics.

    Never raises. Returns None for requests whose model isn't in the body
    (e.g. Bedrock, where it's in the path and already shown via the subpath).
    """
    try:
        model = json.loads(body).get("model")
        return model if isinstance(model, str) else None
    except Exception:
        return None


def proxy_request(provider, subpath, flask_request):
    body = flask_request.get_data()
    ctx = Ctx(
        provider=provider.name,
        method=flask_request.method,
        subpath=subpath,
        body=body,
        headers=dict(flask_request.headers),
        t0=time.monotonic(),
        model=_guess_model(body),
    )
    hooks.on_request(ctx)

    pr = provider.forward(flask_request, subpath, body)

    hooks.on_response(ctx, pr)
    return Response(pr.content, status=pr.status, headers=pr.headers)
