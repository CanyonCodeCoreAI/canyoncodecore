# Shared schema for the consolidated future:{future_id} Redis hash.
#
# IDENTITY_FIELDS are written once by the future's creator (the origin) and
# must never be overwritten by a remote execution snapshot. EXECUTION_FIELDS
# are produced during/after execution and are always last-write-wins when
# merged in from a remote node's callback -- the origin has no independent
# opinion about e.g. cpu_resource or finished_at.

IDENTITY_FIELDS = [
    "id",
    "request_id",
    "parent",
    "service",
    "method",
    "args",
    "created_at",
]

EXECUTION_FIELDS = [
    "result",
    "error",
    "failed",
    "error_message",
    "finished_at",
    "cpu_resource",
    "gpu_resource",
    "agent",
    "queue_time",
    "model",
    "input_token_count",
    "output_token_count",
    "token_count",
    "errors",
    "input_cache_tokens",
    "input_cache_write_tokens",
]


def snapshot_execution_fields(redis, future_id):
    """Read the execution-related fields currently stored for a future."""
    data = redis.hgetall(f"future:{future_id}")
    return {k: v for k, v in data.items() if k in EXECUTION_FIELDS}


def merge_execution_snapshot(redis, future_id, snapshot):
    """Merge a remote execution snapshot into this node's future:{future_id} hash.

    Only EXECUTION_FIELDS are applied -- identity fields in the incoming
    snapshot (if any) are ignored so a remote node can never clobber the
    origin's own record of what the future is. None values are dropped
    rather than merged, so an absent/unset field on the sender doesn't
    stomp a value already present on the receiver.
    """
    filtered = {
        k: v
        for k, v in snapshot.items()
        if k in EXECUTION_FIELDS and v is not None
    }
    if filtered:
        redis.hset_multiple(f"future:{future_id}", filtered)
