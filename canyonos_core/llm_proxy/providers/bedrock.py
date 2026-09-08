"""Bedrock adapter.

TLDR: User code calls boto3 which requires certain format, but sends requests to llm-proxy, which has its own boto3 that makes/recieves requests. But being a middleman, we need to decrypt the messages to get contents, and then re-encrypt so the users boto3 call receives the correct format.

Rather than re-sign the caller's SigV4 request (fiddly once model IDs contain
``:`` and ``/``), we re-issue the call through the proxy's own boto3 client,
which handles signing and URL-encoding correctly by construction.

``converse-stream`` is supported: boto3's ``converse_stream`` already decodes
the upstream AWS event-stream response into plain dicts, so we re-encode those
back into the same ``application/vnd.amazon.eventstream`` wire format so the
caller's own boto3 client (pointed at us via
``AWS_ENDPOINT_URL_BEDROCK_RUNTIME``) can decode it exactly as if it had hit
Bedrock directly. ``invoke-with-response-stream`` (raw per-model streaming, as
opposed to the unified Converse API) remains out of scope.
"""

from __future__ import annotations

import json
import struct
import zlib

import boto3
from botocore.exceptions import ClientError

from canyonos_core.llm_proxy.providers.base import Provider, ProxyResponse

# bedrock-runtime operations that can appear as the last path segment ("invoke-with-response-stream" remains out of scope).
_SUPPORTED_OPS = {"invoke", "invoke-with-response-stream", "converse", "converse-stream"}

# Header value type ID for "string" from the AWS event-stream binary format spec (the only type Bedrock's headers use).
_HEADER_TYPE_STRING = 7


def _encode_event_headers(headers: dict) -> bytes:
    """Pack event-stream headers: [1B name len][name][1B type][2B value
    len][value], repeated. Mirrors what botocore.eventstream decodes."""
    buf = bytearray()
    for name, value in headers.items():
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        buf.append(len(name_bytes))
        buf.extend(name_bytes)
        buf.append(_HEADER_TYPE_STRING)
        buf.extend(struct.pack(">H", len(value_bytes)))
        buf.extend(value_bytes)
    return bytes(buf)


def _encode_event(headers: dict, payload: bytes) -> bytes:
    """Encode one AWS event-stream frame (botocore only decodes this format, never encodes it)."""
    header_bytes = _encode_event_headers(headers)
    total_length = 8 + 4 + len(header_bytes) + len(payload) + 4
    prelude = struct.pack(">II", total_length, len(header_bytes))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + prelude_crc + header_bytes + payload
    message_crc = struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)
    return message + message_crc


def _event_frame(event_type: str, body: dict) -> bytes:
    """Encode a normal Bedrock ConverseStream event (e.g. messageStart, contentBlockDelta) as a frame."""
    headers = {
        ":event-type": event_type,
        ":content-type": "application/json",
        ":message-type": "event",
    }
    return _encode_event(headers, json.dumps(body).encode("utf-8"))


def _exception_frame(exception_type: str, message: str) -> bytes:
    """Encode a mid-stream error as a Bedrock ConverseStream exception frame."""
    headers = {
        ":exception-type": exception_type,
        ":content-type": "application/json",
        ":message-type": "exception",
    }
    return _encode_event(headers, json.dumps({"message": message}).encode("utf-8"))


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

            elif op == "converse-stream":
                params = json.loads(body)
                params["modelId"] = model_id
                resp = self._client.converse_stream(**params)

                pr = ProxyResponse(
                    status=resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 200),
                    headers=[("Content-Type", "application/vnd.amazon.eventstream")],
                )
                pr.stream = self._encode_converse_stream(resp["stream"], pr)
                return pr
            else:
                raise NotImplementedError(
                    f"bedrock op '{op}' not supported (only invoke, converse, and converse-stream)"
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
    def _encode_converse_stream(events, pr: ProxyResponse):
        """Re-frame boto3's already-decoded ConverseStream events
        (``{"messageStart": {...}}``, ``{"contentBlockDelta": {...}}``, ...,
        finally ``{"metadata": {"usage": {...}}}``) back into the AWS
        event-stream wire format the caller's own boto3 client expects.

        Also captures usage off the trailing "metadata" event onto ``pr`` (read
        by hooks.on_response only after this generator is exhausted, since
        usage isn't known until then) and turns any mid-stream failure into a
        single exception frame instead of dropping the connection.
        """
        try:
            for event in events:
                event_type, event_body = next(iter(event.items()))
                if event_type == "metadata":
                    pr.stream_usage = event_body.get("usage")
                yield _event_frame(event_type, event_body)
        except ClientError as exc:
            pr.stream_error = True
            err = exc.response.get("Error", {})
            yield _exception_frame(
                err.get("Code", "InternalServerException"),
                err.get("Message", str(exc)),
            )
        except Exception as exc:  # noqa: BLE001 - surface any mid-stream failure as an exception frame instead of truncating silently
            pr.stream_error = True
            yield _exception_frame(type(exc).__name__, str(exc))

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
