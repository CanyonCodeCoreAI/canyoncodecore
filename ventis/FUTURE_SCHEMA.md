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
| `error`                     | `future.py` (`_submit_request`), `local_controller.py` (`_mark_future_failed`) -- the sole failure-message field; `bedrock.py` deliberately never writes it |
| `finished_at`               | `local_controller.py` (`_execute_locally` finally block) |
| `cpu_resource`              | `local_controller.py` |
| `gpu_resource`              | `local_controller.py` |
| `agent`                     | `local_controller.py` (agent_id that executed this step) |
| `queue_time`                | `local_controller.py` (only when `submitted_at` is known) |
| `model`                     | `llm/bedrock.py` (`call_bedrock`) |
| `input_token_count`         | `llm/bedrock.py` |
| `output_token_count`        | `llm/bedrock.py` |
| `token_count`               | `llm/bedrock.py` |
| `errors`                    | `llm/bedrock.py` (Bedrock call error count) |
| `input_cache_tokens`        | `llm/bedrock.py` |
| `input_cache_write_tokens`  | `llm/bedrock.py` |
