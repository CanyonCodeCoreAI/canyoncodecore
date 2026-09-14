# `future:{future_id}` Redis hash schema

Both directions (origin -> executor request, executor -> origin completion
callback) send the future's full hash. Whichever node last wrote a field
wins for most fields (e.g. `args` as re-serialized by the executor) -- the
one exception is `created_at`, which only the origin ever writes, so it
always reflects the future's true submission time.

Fields currently written into `future:{future_id}`, and where:

| Field                      | Written by |
|----------------------------|------------|
| `id`                       | `future.py` (`Future.__init__`), `local_controller.py` (`_execute_locally`) |
| `request_id`                | `future.py`, `local_controller.py` |
| `parent`                    | `future.py`, `local_controller.py` |
| `service`                   | `future.py`, `local_controller.py` |
| `method`                    | `future.py`, `local_controller.py` |
| `args`                      | `future.py`, `local_controller.py` (json-encoded) |
| `created_at`                | `future.py` only (origin submission time) |
| `result`                    | `future.py`, `local_controller.py` |
| `failed`                    | `future.py`, `local_controller.py` |
| `error`                     | `future.py` (`_submit_request`), `local_controller.py` (`_mark_future_failed`) -- the sole failure-message field; the LLM proxy deliberately never writes it |
| `finished_at`               | `local_controller.py` (`_execute_locally` finally block) |
| `cpu_resource`              | `local_controller.py` |
| `gpu_resource`              | `local_controller.py` |
| `agent`                     | `local_controller.py` (agent_id that executed this step) |
| `queue_time`                | `local_controller.py` (only when `submitted_at` is known) |
| `model`                     | `llm_proxy/hooks.py` (on_response) |
| `input_token_count`         | `llm_proxy/hooks.py` |
| `output_token_count`        | `llm_proxy/hooks.py` |
| `token_count`               | `llm_proxy/hooks.py` |
| `errors`                    | `llm_proxy/hooks.py` (Bedrock call error flag) |
| `input_cache_tokens`        | `llm_proxy/hooks.py` |
| `input_cache_write_tokens`  | `llm_proxy/hooks.py` |
| `logs`                      | `future.py`, `local_controller.py` (`_mark_future_failed`), `log_handler.py` (`LogHandler`) -- JSON-encoded array of OTel Log Data Model entries; see below |

## The `logs` field

`logs` holds a JSON-encoded array of OTel Log Data Model dicts (`Timestamp`, `SeverityNumber`,
`SeverityText`, `Body`, `Attributes`, `Resource`), appended to (never overwritten) via
`append_log_entry` in `controller/utils/log_entry.py`. Two writers feed it, split strictly by
severity, and neither is optional for the range it covers:

- **WARNING and above** (failures): always written by `_mark_future_failed` and
  `Future._submit_request`'s gRPC-failure handler, via `build_failure_entry`. This happens
  unconditionally -- it is not gated by any flag, because the same call site also sets the
  cheap `error`/`failed` fields, wakes up any consumers waiting on the future, and relays the
  failure to `origin` across instances.
- **DEBUG/INFO only** (ambient context): captured by `LogHandler`, a `logging.Handler` attached
  to the root logger in `LocalController.__init__`, but only when `logs_enabled` is true (the
  `logs:` key in `global_controller.yaml`, off by default). `LogHandler` explicitly refuses to
  handle WARNING and above so it can never duplicate what the failure path already wrote.

When `logs_enabled` is false (the default), only the WARNING-and-above writer ever runs, so a
failed future still gets `error`/`failed` set and consumers/origin still get notified -- it just
has no `logs` entry with the extra detail (message, traceback, agent identity).
