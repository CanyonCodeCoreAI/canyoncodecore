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

from canyonos_core.llm_proxy.providers.base import Provider, ProxyResponse

# bedrock-runtime operations that can appear as the last path segment; only the
# non-streaming "invoke" is wired up for now.
_SUPPORTED_OPS = {"invoke", "invoke-with-response-stream", "converse", "converse-stream"}


class BedrockProvider(Provider):
    name = "bedrock"

    def __init__(self, cfg):
        super().__init__(cfg)
        # Explicitly set endpoint_url to bypass AWS_ENDPOINT_URL_BEDROCK_RUNTIME
        # environment variable that points to this proxy (would create infinite loop)
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.bedrock_region,
            endpoint_url=f"https://{cfg.bedrock_upstream_host}"
        )

    def forward(self, req, subpath, body):
        model_id, op = self._parse(subpath)
        
        try:
            if op == "invoke":
                resp = self._client.invoke_model(
                    modelId=model_id,
                    body=body,
                    contentType=req.headers.get("Content-Type", "application/json"),
                    accept=req.headers.get("Accept", "application/json"),
                )
                # For invoke, return raw response body
                payload = resp["body"].read()
                status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
                headers = [("Content-Type", resp.get("contentType", "application/json"))]
                return ProxyResponse(status=status, headers=headers, content=payload)
                
            elif op == "converse":
                params = json.loads(body)
                params["modelId"] = model_id
                resp = self._client.converse(**params)
                
                # Return response as JSON
                response_data = {
                    "output": resp.get("output", {}),
                    "stopReason": resp.get("stopReason"),
                    "usage": resp.get("usage", {}),
                }
                # Include optional fields if present
                for field in ["metrics", "trace", "additionalModelResponseFields"]:
                    if field in resp:
                        response_data[field] = resp[field]
                
                payload = json.dumps(response_data).encode("utf-8")
                status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
                return ProxyResponse(
                    status=status,
                    headers=[("Content-Type", "application/json")],
                    content=payload
                )
            else:
                raise NotImplementedError(
                    f"bedrock op '{op}' not supported (only invoke and converse)"
                )
                
        except ClientError as exc:
            return self._error_response(exc)
        except (json.JSONDecodeError, KeyError) as exc:
            return ProxyResponse(
                status=400,
                headers=[("Content-Type", "application/json")],
                content=json.dumps({"message": f"Invalid request: {exc}"}).encode(),
            )
    


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
