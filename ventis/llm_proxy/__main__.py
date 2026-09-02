"""Entry point: ``python -m llm_proxy``."""

from __future__ import annotations

import logging

from ventis.llm_proxy.app import create_app
from ventis.llm_proxy.config import Config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    app = create_app(cfg)
    logging.getLogger("llm_proxy").info(
        "llm_proxy on http://%s:%d  (openai=%s, anthropic=%s, bedrock=%s [%s])",
        cfg.host, cfg.port, cfg.openai.upstream_base, cfg.anthropic.upstream_base,
        cfg.bedrock_upstream_host, cfg.bedrock_region,
    )
    # threaded so concurrent callers don't serialize; dev server is fine for a
    # local proxy.
    app.run(host=cfg.host, port=cfg.port, threaded=True)


if __name__ == "__main__":
    main()
