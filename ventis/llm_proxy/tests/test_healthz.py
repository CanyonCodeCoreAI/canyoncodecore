"""Tests for /healthz, including the opt-in per-model deep check (CAN-287)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from ventis.llm_proxy.app import create_app
from ventis.llm_proxy.config import Config, ProviderConfig


def make_cfg(openai_key="sk-openai", anthropic_key="sk-ant"):
    return Config(
        host="127.0.0.1",
        port=8080,
        connect_timeout=1.0,
        read_timeout=1.0,
        openai=ProviderConfig(upstream_base="https://api.openai.com", api_key=openai_key),
        anthropic=ProviderConfig(upstream_base="https://api.anthropic.com", api_key=anthropic_key),
        bedrock_region="us-east-1",
        bedrock_upstream_host="bedrock-runtime.us-east-1.amazonaws.com",
        redis_host="localhost",
        redis_port=6379,
    )


@pytest.fixture
def client():
    app = create_app(make_cfg())
    app.testing = True
    return app.test_client()


def test_healthz_without_params_is_unchanged(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"status": "ok", "providers": ["anthropic", "bedrock", "openai"]}


def test_healthz_model_check_ok(client, monkeypatch):
    ok_resp = MagicMock(status_code=200)
    monkeypatch.setattr("ventis.llm_proxy.providers.base.requests.get", lambda *a, **k: ok_resp)

    resp = client.get("/healthz?openai_model=gpt-4o-mini")
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["models"] == {"openai": {"model": "gpt-4o-mini", "ok": True}}


def test_healthz_model_check_reports_404_as_degraded(client, monkeypatch):
    not_found = MagicMock(status_code=404)
    monkeypatch.setattr("ventis.llm_proxy.providers.base.requests.get", lambda *a, **k: not_found)

    resp = client.get("/healthz?anthropic_model=claude-ancient")
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["models"]["anthropic"] == {
        "model": "claude-ancient",
        "ok": False,
        "error": "upstream returned 404",
    }
    # unrequested providers are left alone
    assert "openai" not in body["models"]


def test_healthz_model_check_without_api_key_fails_fast():
    app = create_app(make_cfg(openai_key=None))
    resp = app.test_client().get("/healthz?openai_model=gpt-4o-mini")
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["models"]["openai"] == {
        "model": "gpt-4o-mini",
        "ok": False,
        "error": "no API key configured",
    }


def _patch_bedrock_clients(monkeypatch, control_client):
    """boto3.client("bedrock-runtime", ...) is unused by /healthz; only the
    control-plane ("bedrock") client needs a real double."""

    def fake_client(service_name, **kwargs):
        return control_client if service_name == "bedrock" else MagicMock()

    monkeypatch.setattr("ventis.llm_proxy.providers.bedrock.boto3.client", fake_client)


def test_bedrock_check_model_ok(monkeypatch):
    control = MagicMock(get_foundation_model=MagicMock(return_value={}))
    _patch_bedrock_clients(monkeypatch, control)

    app = create_app(make_cfg())
    resp = app.test_client().get("/healthz?bedrock_model=anthropic.claude-3-5-sonnet-20240620-v1:0")
    body = resp.get_json()
    assert body["models"]["bedrock"]["ok"] is True
    assert "invoke access" in body["models"]["bedrock"]["note"]


def test_bedrock_check_model_not_found(monkeypatch):
    error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "model not found"}},
        "GetFoundationModel",
    )
    control = MagicMock(get_foundation_model=MagicMock(side_effect=error))
    _patch_bedrock_clients(monkeypatch, control)

    app = create_app(make_cfg())
    resp = app.test_client().get("/healthz?bedrock_model=made-up-model")
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert body["models"]["bedrock"] == {
        "model": "made-up-model",
        "ok": False,
        "error": "model not found",
    }
