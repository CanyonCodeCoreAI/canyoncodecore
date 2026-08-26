# Traps

Symptom-to-cause lookup for a port that is already written. The mechanism behind
each row is in `ventis-contract.md`.

## Before any container starts


| Symptom                                               | Cause                                                                                                    |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `env_file does not exist` on deploy                   | the path is resolved against the project root you run from; `cmd_deploy` fails before launching anything |
| `int() argument must be ... not 'NoneType'` on deploy | `provider:` is not lowercase `local`, so no host port was reserved                                       |
| `generated grpc_stubs are missing or not importable`  | `ventis build` has not run on this host, or its output was cleaned                                       |
| An agent missing from the deployment                  | its config `name` matched no yaml; the build logged a warning and exited 0                               |


## The container dies or serves nothing


| Symptom                                            | Cause                                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Container exits on `import local_controller`       | protobuf gencode newer than the resolved runtime; nothing pins the gRPC stack                  |
| `"No agent loaded"` on the first request           | anything below — the agent container's stdout is the only place the cause exists               |
| A replica reports `healthy` but answers nothing    | same; `healthy` is written before `_load_agent` runs and is never revised                      |
| `Missing credentials` loading the agent            | no `env_file:`, or the key the source reads is not in it                                       |
| `ModuleNotFoundError` for the source's own modules | the project declares no packaging metadata, so `-e .` was skipped and only flat modules import |
| `ModuleNotFoundError` for a third-party package    | an import the source needs is missing from the entry's `requirements:`                         |
| `NameError` importing a stub                       | a yaml `type` that is not a builtin                                                            |


## The request is accepted and then goes wrong


| Symptom                                             | Cause                                                                                 |
| --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `TypeError: unexpected keyword argument`            | yaml `arguments[].name` != the Python parameter name                                  |
| `Unauthorized: Policy denied access to service 'X'` | `X` is missing from the `access` list of the policy rule that matched                 |
| `.value()` returns a `str` of a dict                | expected — `json.loads` it                                                            |
| `Object of type ... is not JSON serializable`       | the adapter returned framework objects; serialize with the framework's own serializer |
| Redis holds `<coroutine object ...>`                | the method is `async def`; keep the signature sync and `asyncio.run` inside           |
| No faster than the original                         | calls fused with `.value()`; dispatch all, then resolve all                           |
| Debug code runs in production                       | the workflow is `exec`'d, so `__name__ == "__main__"`                                 |



## Through the deployment platform's test endpoint

| Symptom                                             | Cause                                                                                       |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 404 from the test endpoint, container healthy       | the workflow function is not named `main`; the platform posts to a hardcoded `/main`          |
| 400 before the request reaches the host             | the body key is not `query`; the platform's schema is strict and rejects everything else      |
| The workflow runs but an argument is missing        | only `query` is ever sent; every other parameter needs a default                             |
