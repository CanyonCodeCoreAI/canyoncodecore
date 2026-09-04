"""The metrics seam.

Every proxied call passes through ``on_request`` / ``on_response``. Today these
only log. Token accounting lands here later: because the whole response is
buffered, usage extraction is a one-liner, e.g. ``resp.json().get("usage")`` for
OpenAI/Anthropic (Bedrock's usage lives in its per-model response body).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger("llm_proxy")


@dataclass
class Ctx:
    provider: str
    method: str
    subpath: str
    body: bytes
    headers: Dict[str, str]
    t0: float
    model: Optional[str] = None

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000.0


class Hooks:
    def on_request(self, ctx: Ctx) -> None:
        log.info(
            "→ %s %s /%s model=%s (%d bytes)",
            ctx.provider, ctx.method, ctx.subpath, ctx.model, len(ctx.body),
        )

    def on_response(self, ctx: Ctx, resp: Any) -> None:
        log.info(
            "← %s %s /%s -> %s in %.0fms",
            ctx.provider, ctx.method, ctx.subpath,
            getattr(resp, "status", "?"), ctx.elapsed_ms(),
        )
        # TODO(metrics): pull token usage off `resp` and emit it.


hooks = Hooks()
