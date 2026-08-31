# `future:{future_id}` Redis hash schema

Both directions (origin -> executor request, executor -> origin completion
callback) send the future's full hash. Whichever node last wrote a field
wins for most fields (e.g. `args` as re-serialized by the executor) -- the
one exception is `created_at`, which only the origin ever writes, so it
always reflects the future's true submission time.

`future:{future_id}` is the hash; `:children` and `:consumers` are separate
sibling-key sets sharing the same `future:{future_id}` prefix (a hash field
can't hold a collection). Nothing hardcodes that suffix list -- `_cleanup_request`
deletes everything found by `scan_keys(f"future:{future_id}*")`, and the
telemetry poller's `pull_runtime_information` treats any key with more than
one `:` as a non-hash sibling key and skips it. Adding a new sibling key
needs no changes to either of those.

Fields currently written into `future:{future_id}`, and where:

| Key                              | Type | Field                     | Written by |
|----------------------------------|------|----------------------------|------------|
| `future:{future_id}`              | hash | `id`                       | `future.py` (`Future.__init__`), `local_controller.py` (`_execute_locally`) |
| `future:{future_id}`              | hash | `request_id`                | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `parent`                    | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `service`                   | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `method`                    | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `args`                      | `future.py`, `local_controller.py` (json-encoded) |
| `future:{future_id}`              | hash | `created_at`                | `future.py` only (origin submission time) |
| `future:{future_id}`              | hash | `result`                    | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `failed`                    | `future.py`, `local_controller.py` |
| `future:{future_id}`              | hash | `error`                     | `future.py` (`_submit_request`), `local_controller.py` (`_mark_future_failed`) -- latest failure's exception type name only (e.g. `ValueError`); `bedrock.py` deliberately never writes it |
| `future:{future_id}`              | hash | `finished_at`               | `local_controller.py` (`_execute_locally` finally block) |
| `future:{future_id}`              | hash | `cpu_resource`              | `local_controller.py` |
| `future:{future_id}`              | hash | `gpu_resource`              | `local_controller.py` |
| `future:{future_id}`              | hash | `agent`                     | `local_controller.py` (agent_id that executed this step) |
| `future:{future_id}`              | hash | `queue_time`                | `local_controller.py` (only when `submitted_at` is known) |
| `future:{future_id}`              | hash | `model`                     | `llm/bedrock.py` (`call_bedrock`) |
| `future:{future_id}`              | hash | `input_token_count`         | `llm/bedrock.py` |
| `future:{future_id}`              | hash | `output_token_count`        | `llm/bedrock.py` |
| `future:{future_id}`              | hash | `token_count`               | `llm/bedrock.py` |
| `future:{future_id}`              | hash | `errors`                    | `llm/bedrock.py` (Bedrock call error count -- an int, unrelated to `logs` below) |
| `future:{future_id}`              | hash | `input_cache_tokens`        | `llm/bedrock.py` |
| `future:{future_id}`              | hash | `input_cache_write_tokens`  | `llm/bedrock.py` |
| `future:{future_id}`              | hash | `logs`                      | `local_controller.py` (`_mark_future_failed`), `future.py` (`_submit_request`), via `append_log_entry()` in `ventis/utils/log_entry.py` -- a JSON-encoded array of OTel Log Data Model records (`Timestamp`, `SeverityNumber`, `SeverityText`, `Body`, `Attributes`, `Resource`; built by `build_failure_entry()`). `local_controller.py`'s `LogHandler` also appends here for any DEBUG/INFO logging emitted while this future is executing (`build_log_entry()`, same module) -- it skips WARNING and above since those levels are already covered by the deterministic failure path above, and would otherwise double-record the same event. So (unlike `error`) the full history survives repeated failures. Only populated when `global_controller.yaml`'s `logs:` flag is on (`VENTIS_LOGS_ENABLED`, off by default) -- streaming every log line through the future object can clog bandwidth, so `error`/`failed` above are the only fields written unconditionally. Read-modify-write on every append, not atomic. Being a plain hash field, it crosses the cross-instance completion callback for free -- `_send_result_callback`/`WriteResult` never special-case it. Not yet persisted to `runtime_information`. |
| `future:{future_id}:consumers`    | set  | endpoints awaiting this future's result | `local_controller.py` (`_process_request`, when forwarding to a remote endpoint), `future.py` (`add_consumer`/`remove_consumer`) |
| `future:{future_id}:children`     | set  | child future ids           | `future.py` (`_children_key`) defines this key but nothing currently `sadd`s into it -- dead/unused today |
