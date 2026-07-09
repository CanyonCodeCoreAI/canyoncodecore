# Example output policies for FinanceAgent.
#
# An output policy is any callable with the signature:
#
#     policy(output, ctx) -> None
#
# - `output` is the agent method's return value.
# - `ctx`    carries request metadata: at least `request_id`, `service`,
#            `function`, plus whatever request context was propagated
#            (e.g. `origin`, `tags`).
#
# Policies are SIDE-EFFECT ONLY: they observe/react to the output (audit,
# notify, enforce) but do not modify it. Any return value is ignored, so the
# policies are independent of one another and their order does not affect the
# result. The framework only finds and runs the configured policies; the
# decision of whether/how to act lives inside each policy.
#
# Bind them in the controller config:
#
#     agents:
#       - name: FinanceAgent
#         output_policies:
#           - policies.finance:audit_log
#           - policies.finance:alert_on_sensitive

import logging

logger = logging.getLogger(__name__)

# Toy list of tokens we never want to leak in a response.
_SENSITIVE = ("SSN", "password", "api_key")


def audit_log(output, ctx):
    """Record every result for auditing. Does not modify the output."""
    logger.info(
        "[audit] request=%s %s.%s -> %r",
        ctx.get("request_id"),
        ctx.get("service"),
        ctx.get("function"),
        output,
    )


def alert_on_sensitive(output, ctx):
    """Warn if a result appears to contain sensitive tokens.

    Self-guards: only inspects string output; anything else is ignored.
    Reacts (logs a warning) but does not change the output.
    """
    if not isinstance(output, str):
        return
    for token in _SENSITIVE:
        if token in output:
            logger.warning(
                "[alert] sensitive token %r in result of %s.%s (request=%s)",
                token,
                ctx.get("service"),
                ctx.get("function"),
                ctx.get("request_id"),
            )
