"""Flask app: one catch-all route per provider prefix, all funneled through
``proxy_request``."""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from ventis.llm_proxy.config import Config
from ventis.llm_proxy.core import proxy_request
from ventis.llm_proxy.providers import build_registry

log = logging.getLogger("llm_proxy")

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_app(cfg: Config = None) -> Flask:
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    registry = build_registry(cfg)
    
    # Initialize hooks with config for Redis
    from ventis.llm_proxy import hooks as hooks_module
    hooks_module.hooks = hooks_module.Hooks(cfg)

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify(status="ok", providers=sorted(registry.keys()))

    @app.route("/<provider>/<path:subpath>", methods=ALL_METHODS)
    def dispatch(provider, subpath):
        prov = registry.get(provider)
        if prov is None:
            return (
                jsonify(error=f"unknown provider '{provider}'", known=sorted(registry.keys())),
                404,
            )
        try:
            return proxy_request(prov, subpath, request)
        except Exception as exc:  # surface upstream/adapter errors as 502
            log.exception("proxy error for %s/%s", provider, subpath)
            return jsonify(error="proxy_error", detail=str(exc)), 502

    return app
