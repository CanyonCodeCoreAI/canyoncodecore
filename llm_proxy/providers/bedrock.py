"""Bedrock adapter.

Rather than re-sign the caller's SigV4 request (fiddly once model IDs contain
``:`` and ``/``), we re-issue the call through the proxy's own boto3 client,
which handles signing and URL-encoding correctly by construction. This is clean
for request/response; streaming (``invoke-with-response-stream``) is out of scope
for now.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from llm_proxy.providers.base import Provider, ProxyResponse

# bedrock-runtime operations that can appear as the last path segment; only the
# non-streaming "invoke" is wired up for now.
_SUPPORTED_OPS = {"invoke", "invoke-with-response-stream", "converse", "converse-stream"}


class BedrockProvider(Provider):
    name = "bedrock"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._client = boto3.client("bedrock-runtime", region_name=cfg.bedrock_region)

    def forward(self, req, subpath, body):
        model_id, op = self._parse(subpath)
        if op != "invoke":
            raise NotImplementedError(
                f"bedrock op '{op}' not supported yet (streaming/converse are out of scope)"
            )
        try:
            resp = self._client.invoke_model(
                modelId=model_id,
                body=body,
                contentType=req.headers.get("Content-Type", "application/json"),
                accept=req.headers.get("Accept", "application/json"),
            )
        except ClientError as exc:
            return self._error_response(exc)

        payload = resp["body"].read()
        status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
        headers = [("Content-Type", resp.get("contentType", "application/json"))]
        return ProxyResponse(status=status, headers=headers, content=payload)

    @staticmethod
    def _parse(subpath):
        # subpath looks like "model/<modelId>/<op>"; the modelId may itself
        # contain "/" (inference-profile ARNs), so peel the op off the right.
        if not subpath.startswith("model/"):
            raise ValueError(f"unrecognized bedrock path: /{subpath}")
        model_id, sep, op = subpath[len("model/"):].rpartition("/")
        if not sep or op not in _SUPPORTED_OPS:
            raise ValueError(f"unrecognized bedrock path: /{subpath}")
        return model_id, op

    @staticmethod
    def _error_response(exc: ClientError) -> ProxyResponse:
        # boto3 raises on 4xx/5xx; reconstruct a JSON error body carrying the
        # real status + message. (Byte-for-byte error passthrough is a property
        # only the HTTP providers have; this is the cost of re-issuing via boto3.)
        meta = exc.response.get("ResponseMetadata", {})
        err = exc.response.get("Error", {})
        status = meta.get("HTTPStatusCode", 500)
        body = json.dumps(
            {"message": err.get("Message", str(exc)), "code": err.get("Code")}
        ).encode("utf-8")
        return ProxyResponse(
            status=status,
            headers=[("Content-Type", "application/json")],
            content=body,
        )
