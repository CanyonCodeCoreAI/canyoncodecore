from __future__ import annotations

from canyonos_core.llm_proxy.providers.base import HttpProvider, UpstreamRequest, client_headers


class OpenAIProvider(HttpProvider):
    name = "openai"

    def target(self, req, subpath, body):
        headers = client_headers(req, drop=["authorization"])
        if self.cfg.openai.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.openai.api_key}"
        return UpstreamRequest(
            method=req.method,
            url=f"{self.cfg.openai.upstream_base}/{subpath}",
            headers=headers,
            params=req.args.to_dict(flat=True),
        )
