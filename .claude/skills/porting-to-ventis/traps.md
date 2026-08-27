# Traps

Symptom-to-cause lookup for a port that is already written. The mechanism behind
each row is in `ventis-contract.md`. Rows marked with a check id are decided
before any of this happens by `validate.py`.

## Before any container starts


| Symptom                                               | Cause                                                                                                    |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `env_file does not exist` on deploy                   | the path is resolved against the project root you run from; `cmd_deploy` fails before launching anything (V030) |
| `int() argument must be ... not 'NoneType'` on deploy | `provider:` is not lowercase `local`, so no host port was reserved (V012)                                |
| `TypeError: int() argument ...` naming `replicas`     | `replicas:` is a list; `_get_replica_placements` accepts that shape but `InstanceManager` calls `int()` on it (V013) |
| `AttributeError` inside `GlobalController.__init__`   | `config/policy.yaml` exists but is empty, or its `rules:` is null. Absent would have been fine (V021)     |
| `EC2 deploy preflight failed: missing ec2 config keys`| no top-level `ec2:` block, or an incomplete one. `ssh_user` passes the CLI's shorter list and fails later at provision (V022) |
| `generated grpc_stubs are missing or not importable`  | `ventis build` has not run on this host, or its output was cleaned                                       |
| An agent missing from the deployment                  | its config `name` matched no yaml, or its entry has no `entrypoint`; the build logged a warning and exited 0 (V003, V005) |
| Two agents, one image                                 | two config `name`s differing only in case — both tag `ventis-<name.lower()>` and the second overwrites the first (V004) |


## The container dies or serves nothing


| Symptom                                            | Cause                                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Container exits on `import local_controller`       | protobuf gencode newer than the resolved runtime; nothing pins the gRPC stack                  |
| `"No agent loaded"` on the first request           | anything below — the agent container's stdout is the only place the cause exists               |
| A replica reports `healthy` but answers nothing    | same; `healthy` is written before `_load_agent` runs and is never revised                      |
| `Missing credentials` loading the agent            | no `env_file:`, or the key the source reads is not in it                                       |
| `ModuleNotFoundError` for the source's own modules | only modules that land flat at `/app` import; on a Ventis with `-e .`, the project also declares no packaging metadata (V031) |
| `ModuleNotFoundError` for a third-party package    | an import the source needs is missing from the entry's `requirements:` (W006)                  |
| `ModuleNotFoundError` for nothing in particular    | `requirements:` was not a list of strings, so the whole list was dropped with one warning (V014) |
| `NameError` importing a stub                       | a yaml `type` that is not a builtin (V010)                                                     |
| A peer's real code behaves like an empty stub      | a project module at the root shares a basename with an `agents/*.yaml`, and the stub is copied over it (V020) |
| The container dies on a Ventis module name         | a project module at the root is called `local_controller.py`, `deploy.py`, `future.py` ... — the runtime is copied flat over it (V019) |


## The request is accepted and then goes wrong


| Symptom                                             | Cause                                                                                 |
| --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `TypeError: unexpected keyword argument`            | yaml `arguments[].name` != the Python parameter name (V008)                           |
| `Unauthorized: Policy denied access to service 'X'` | `X` is missing from the `access` list of the policy rule that matched (V021)          |
| `.value()` returns a `str` of a dict                | expected — `json.loads` it                                                            |
| `Object of type ... is not JSON serializable`       | the adapter returned framework objects; serialize with the framework's own serializer |
| Redis holds `<coroutine object ...>`                | the method is `async def`; keep the signature sync and `asyncio.run` inside (V009)    |
| No faster than the original                         | calls fused with `.value()`; dispatch all, then resolve all (V018)                    |
| Debug code runs in production                       | the workflow is `exec`'d, so `__name__ == "__main__"` (V017)                          |



## Through the deployment platform's test endpoint

| Symptom                                             | Cause                                                                                       |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 404 from the test endpoint, container healthy       | the workflow function is not named `main`; the platform posts to a hardcoded `/main` (V016)  |
| 400 before the request reaches the host             | the body key is not `query`; the platform's schema is strict and rejects everything else (V016) |
| The workflow runs but an argument is missing        | only `query` is ever sent; every other parameter needs a default (V016)                      |
