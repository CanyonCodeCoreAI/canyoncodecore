"""The single choke point every proxied call flows through."""

from __future__ import annotations

import json
import time
from typing import Optional

from flask import Response, stream_with_context

from canyonos_core.llm_proxy.hooks import Ctx


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
    # Import hooks here to get the instance created by create_app
    from canyonos_core.llm_proxy.hooks import hooks
    
    body = flask_request.get_data()
    ctx = Ctx(
        provider=provider.name,
        method=flask_request.method,
        subpath=subpath,
        body=body,
        # Lowercased so the case-insensitive HTTP header contract survives past
        # this point: dict(flask_request.headers) renders WSGI's underscored
        # HTTP_X_CANYONOS_FUTURE_ID env var back as 'X-Canyonos-Future-Id' (only
        # the leading letter of each hyphen-segment capitalized), which silently
        # never matches an exact-case "X-Canyonos-Future-ID" lookup downstream.
        headers={k.lower(): v for k, v in flask_request.headers.items()},
        t0=time.monotonic(),
        model=_guess_model(body),
    )
    hooks.on_request(ctx)

    # Test mode: if CANYONOS_LLM_STUB_TEXT is set, return canned text instead of
    # calling the real upstream. Telemetry hooks still fire so the whole pipeline
    # is exercised end-to-end without cloud credentials.
    from canyonos_core.llm_proxy.stub import build_stub, stub_text

    # Empty string (the runtime's explicit "disabled" value) is falsy, so only a
    # non-empty stub text -- which only `canyonos test` sets -- enables stubbing.
    _stub = stub_text()
    if _stub:
        pr = build_stub(provider.name, subpath, _stub)
    else:
        pr = provider.forward(flask_request, subpath, body)

    if pr.stream is not None:
        # Streamed responses: relay chunks as they arrive, and only take telemetry when the whole response is done.
        def relay():
            try:
                yield from pr.stream
            finally:
                hooks.on_response(ctx, pr)

        return Response(
            stream_with_context(relay()),
            status=pr.status,
            headers=pr.headers,
            direct_passthrough=True,
        )

    hooks.on_response(ctx, pr)
    return Response(pr.content, status=pr.status, headers=pr.headers)
