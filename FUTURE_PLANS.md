# Future State Synchronization Plan

## Purpose

Ensure that the origin controller's `future:{future_id}` record contains the
complete future state, including execution metrics and failure details, while
preserving the executor-local metrics used by the local controller and runtime
reporting.

## Current architecture

The origin creates a future record in its Redis instance:

```text
future:{future_id}
```

When a request is forwarded to another EC2 instance, the executor receives the
future ID and request data, not the origin's Redis hash or a live Redis object.
Because Redis keys are scoped to a Redis server, the executor's:

```text
future:{future_id}
```

is a different Redis hash from the origin's identically named key.

The executor also creates:

```text
future:{future_id}:metrics
```

This hash contains execution-specific data, including:

- `id`
- `request_id`
- `service`
- `method`
- `args`
- `created_at`
- `finished_at`
- `failed`
- `error_message`
- `cpu_resource`
- `gpu_resource`
- `agent`
- queue timing
- LLM model and token usage fields, when applicable

The executor additionally updates its controller-level metrics hash:

```text
controller:{host}:{port}:metrics
```

That hash contains aggregate controller data such as requests served and full
failures. It is separate from the per-future metrics hash and should remain so.

## Current result handoff

The executor currently sends this callback payload to the origin:

```json
{
  "future_id": "<future ID>",
  "result": "<serialized result or failure string>"
}
```

The callback is created in `LocalController._send_result_callback()` and
received by `WriteResult()` in the local controller frontend. The origin writes
the received result into its local `future:{future_id}` hash.

The callback currently does not send:

- `failed`
- `error_message`
- the `future:{future_id}:metrics` hash
- CPU/GPU/timing fields
- LLM usage fields

Therefore, the executor's metrics remain only in the executor's Redis instance.

## Current failure behavior

Execution starts by initializing the per-future metrics hash with `failed = 0`
and an empty `error_message`.

Failure metadata is written for:

- exceptions raised by the agent method
- Bedrock call failures
- request-processing failures with a known future ID
- forwarding failures
- result-callback failures

The failure record is:

```text
future:{future_id}:metrics.failed = 1
future:{future_id}:metrics.error_message = str(exception)
```

`Future.value()` checks this metrics hash and raises the recorded error when
the metrics are available in the Redis instance being queried.

For remote execution with separate Redis instances, the origin cannot see the
executor's metrics hash. It receives only the failure result string, so the
origin may return that string instead of raising the original `error_message`.

Malformed JSON cannot be associated with a future because no reliable future ID
is available. Failures before a future ID exists have the same limitation.

## Important callback ordering issue

The executor currently sends the success or failure callback before it writes
all final metrics. In particular, fields such as these are written afterward:

- `finished_at`
- `cpu_resource`
- `gpu_resource`
- `agent`
- `queue_time`

Consequently, sending a metrics snapshot from the callback's current location
would produce an incomplete snapshot. The callback must be sent only after the
final metrics writes are complete.

## Proposed future-state synchronization

Treat the future ID as the logical identity, while explicitly synchronizing a
serialized snapshot between Redis instances.

### Request path

1. The origin creates `future:{future_id}`.
2. The origin sends the future state/request data and future ID to the executor.
3. The executor executes the request using that future ID.

### Completion path

1. The executor initializes and updates its local per-future metrics hash.
2. The executor writes the result or failure state.
3. The executor writes all final timing and resource metrics.
4. The executor reads the completed future state and metrics fields.
5. The executor sends the complete serialized snapshot to the origin.
6. The origin merges the returned fields into its own `future:{future_id}` hash.
7. `Future.value()` on the origin can read the synchronized failure fields and
   raise the original `error_message`.

A callback payload could contain:

```json
{
  "future_id": "<future ID>",
  "future": {
    "result": "<serialized result>",
    "error": "<optional error>"
  },
  "metrics": {
    "failed": 0,
    "error_message": "",
    "created_at": 0,
    "finished_at": 0,
    "cpu_resource": 0,
    "gpu_resource": 0,
    "agent": "..."
  }
}
```

Alternatively, the origin can receive one flattened field map and merge all
fields into `future:{future_id}`. The important requirement is that the
metrics fields be explicitly transferred; matching key names across different
Redis instances does not synchronize them.

## Data ownership

After synchronization:

- The executor keeps `future:{future_id}:metrics` locally for runtime polling
  and local-controller reporting.
- The origin's `future:{future_id}` contains the result plus the synchronized
  metrics needed by callers.
- The controller-level aggregate metrics hash remains local to each controller.
- Child and consumer bookkeeping should not be copied as part of the completed
  future snapshot unless a separate requirement establishes ownership and merge
  semantics for those sets.

## Implementation considerations

- Move success and failure callbacks until after final metrics writes.
- Extend the callback payload to include the completed metrics/state snapshot.
- Merge returned fields into the origin's `future:{future_id}` hash.
- Preserve the executor-local `future:{future_id}:metrics` hash.
- Define whether origin fields or executor fields win if the same field appears
  in both snapshots.
- Ensure callback retries or duplicate callbacks are idempotent.
- Add tests for local execution and separate-origin/executor Redis instances.
- Verify that `Future.value()` raises the synchronized `error_message` for
  remote failures.

## Non-goals

- Making identically named Redis keys automatically shared across instances.
- Moving aggregate controller metrics into the future record.
- Copying `children` or `consumers` bookkeeping without explicit merge rules.
- Raising failures from the local controller based on the metrics flag; failure
  recording belongs in the controller, while failure surfacing belongs in
  `Future.value()`.

