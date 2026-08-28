# Traps

Symptom-to-cause lookup for a port that is already written. The mechanism behind
each row is in `canyonos-core-contract.md`. Rows marked with a check id are decided
before any of this happens by `validate.py`.

## Before any container starts


| Symptom                                               | Cause                                                                                                    |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `env_file does not exist` on deploy                   | the path is resolved against the project root you run from; `cmd_deploy` fails before launching anything (V030) |
| `int() argument must be ... not 'NoneType'` on deploy | `provider:` is not lowercase `local`, so no host port was reserved                                |
| `TypeError: int() argument ...` naming `replicas`     | `replicas:` is a list; `_get_replica_placements` accepts that shape but `InstanceManager` calls `int()` on it |
| `AttributeError` inside `GlobalController.__init__`   | `config/policy.yaml` exists but is empty, or its `rules:` is null. Absent would have been fine     |
| `EC2 deploy preflight failed: missing ec2 config keys`| no top-level `ec2:` block, or an incomplete one. `ssh_user` passes the CLI's shorter list and fails later at provision |
| `generated grpc_stubs are missing or not importable`  | `ventis build` has not run on this host, or its output was cleaned                                       |
| An agent missing from the deployment                  | its config `name` matched no yaml, or its entry has no `entrypoint`; inspect the build warnings |
| Two agents, one image                                 | two config `name`s differing only in case — both tag `ventis-<name.lower()>` and the second overwrites the first |


## The container dies or serves nothing


| Symptom                                            | Cause                                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Container exits on `import local_controller`       | protobuf gencode newer than the resolved runtime; nothing pins the gRPC stack                  |
| `"No agent loaded"` on the first request           | anything below — the agent container's stdout is the only place the cause exists               |
| A replica reports `healthy` but answers nothing    | same; `healthy` is written before `_load_agent` runs and is never revised                      |
| `Missing credentials` loading the agent            | no `env_file:`, or the key the source reads is not in it                                       |
| `ModuleNotFoundError` for the source's own modules | the original import does not resolve from `/app`; when editable installs are supported, add minimal packaging metadata at the port root (nested source metadata is ignored) (V031) |
| `ModuleNotFoundError` for a third-party package    | an import the source needs is missing from the entry's `requirements:` (W006)                  |
| `ModuleNotFoundError` for nothing in particular    | `requirements:` was not a list of strings, so the whole list was dropped with one warning (V014) |
| `NameError` importing a stub                       | a yaml `type` that is not a builtin (V010)                                                     |
| A peer's real code behaves like an empty stub      | a project module at the root shares a basename with an `agents/*.yaml`, and the stub is copied over it (V020) |
| The container dies on a CanyonOS Core module name         | a project module at the root is called `local_controller.py`, `deploy.py`, `future.py` ... — the runtime is copied flat over it (V019) |


## Through `llm_proxy`

| Symptom | Cause |
| --- | --- |
| `Connection refused` at `127.0.0.1:8080` | that address is the agent container itself, not the Docker host; use `host.docker.internal`, bind the proxy to `0.0.0.0`, and avoid the workflow's host port 8080 |
| Proxy `/healthz` works, but the agent cannot connect | the health probe ran on the host; check the base URL from inside the agent image and whether a remote agent host can reach the proxy |
| OpenAI or Anthropic client says its key is missing before making a request | its SDK still requires the normal key variable; give the container a dummy value and give the proxy process the real value separately |
| boto3 says it cannot locate credentials | botocore signs even a custom endpoint request; give the caller dummy AWS credentials while the proxy process retains its own real AWS identity |
| Upstream answers 401 through the proxy | the proxy process has no real provider key; `/healthz` checks registration, not credentials |
| `BEDROCK_UPSTREAM_HOST` appears to be ignored | it is read into `Config` but never passed to the proxy's boto3 client in this implementation |
| JSON `502` with `proxy_error` | proxy routing or provider code raised; inspect `detail` and proxy logs |
| JSON `502` naming `invoke-with-response-stream`, `converse`, or `converse-stream` | the Bedrock adapter implements only `invoke` |
| A streaming OpenAI or Anthropic call hangs or returns a buffered response | this proxy has no streaming implementation; the port is unsupported without changing source behavior |
| Local agents work but EC2 agents cannot connect | `host.docker.internal` on EC2 names each EC2 Docker host, not the deploying machine; expose a reachable proxy or run one per host |
| Proxy fails to bind port 8080 | the workflow API normally publishes the same host port; run the proxy on another port |


## During cleanup

| Symptom | Cause |
| --- | --- |
| `ventis clean` succeeds but containers still run | the command removes generated directories only; stop `ventis deploy` and remove any exact leftovers |
| `ventis clean` succeeds but `ventis-*` images remain | image deletion is not part of `cmd_clean`; remove the exact tags after their containers stop |
| The next deploy says a port or container name is already in use | the previous deploy crashed or was killed before `GlobalController.cleanup` completed |


## The request is accepted and then goes wrong


| Symptom                                             | Cause                                                                                 |
| --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `TypeError: unexpected keyword argument`            | yaml `arguments[].name` != the Python parameter name (V008)                           |
| `Unauthorized: Policy denied access to service 'X'` | `X` is missing from the `access` list of the policy rule that matched |
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
