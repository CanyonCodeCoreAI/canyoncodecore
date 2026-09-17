from __future__ import annotations

from canyonos_core.llm_proxy.providers.base import HttpProvider, UpstreamRequest, client_headers


class AnthropicProvider(HttpProvider):
    name = "anthropic"

    def target(self, req, subpath, body):
        headers = client_headers(req, drop=["x-api-key", "authorization"])
        if self.cfg.anthropic.api_key:
            headers["x-api-key"] = self.cfg.anthropic.api_key
        # `anthropic-version` is supplied by the SDK and passes through untouched.
        return UpstreamRequest(
            method=req.method,
            url=f"{self.cfg.anthropic.upstream_base}/{subpath}",
            headers=headers,
            params=req.args.to_dict(flat=True),
        )
