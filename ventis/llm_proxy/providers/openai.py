from __future__ import annotations

from ventis.llm_proxy.providers.base import HttpProvider, UpstreamRequest, client_headers


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

    def _model_url(self, model_id):
        return f"{self.cfg.openai.upstream_base}/v1/models/{model_id}"

    def _model_check_headers(self):
        if not self.cfg.openai.api_key:
            return None
        return {"Authorization": f"Bearer {self.cfg.openai.api_key}"}
