# Troubleshooting

Read this after a failed build, image probe, deploy, or request. For mechanisms,
read [runtime-contract.md](runtime-contract.md). For proxy or remote-host failures,
read [llm-proxy.md](llm-proxy.md) or [ec2.md](ec2.md).

## Build or deploy stops early

| Symptom | Likely cause |
|---|---|
| `Application source copy not found` | The command ran somewhere other than the application root, or `.car/app/` was never created |
| `Config file not found: .car/config/...` while `.car` exists | The command ran inside `.car`; it belongs one level up |
| Agent image is missing | Config name matched no declaration in `config/`, entrypoint is absent, or build skipped it; inspect build warnings |
| Wrong declaration is used | Two files in `config/` declare the same `agent.name`; the later filename silently wins |
| Two services produce one image | Config names collide after lowercase normalization |
| `generated grpc_stubs are missing` | Build did not complete on this host, or generated output was cleaned before deploy |
| `int(... NoneType)` during local deploy | Local provider was not written exactly as lowercase `local` |
| Replica conversion `TypeError` | `replicas` is not an integer |
| Policy `AttributeError` | Policy exists but is empty or has null/non-list rules; remove it when unrestricted |
| Port or container name already in use | A previous deployment did not complete cleanup |

## Container exits or serves nothing

| Symptom | Likely cause |
|---|---|
| `import local_controller` raises protobuf version error | Host-generated gRPC code is newer than the image's protobuf runtime |
| First request says `No agent loaded` | Entrypoint import, class lookup, or constructor failed and the controller swallowed the exception; inspect container logs |
| Replica is healthy but serves nothing | Health publication does not prove successful agent loading |
| Missing credentials while loading | Env injection is unavailable/misconfigured or the source reads another variable |
| Source module is missing | `.car/app` is not rooted at the source's import root, so the original import does not resolve from `/app`; read [packaging.md](packaging.md) |
| Third-party module is missing | Distribution is absent from source metadata and config requirements |
| Stub import raises `NameError` | yaml argument type is not a bare builtin |
| An agent runs in the workflow process instead of a container | The workflow reached the class by a path other than the agent's `entrypoint`, so it got the real module rather than the stub |
| Calls to one agent reach another | Two config entries share an `entrypoint`, so one stub was written over the other |
| Runtime-named module disappears | Shared runtime copy overwrote a module at the root of the source copy with the same name |
| An application file is missing from the image | Only `.py` files are swept out of the copy unless the `sweeps_all_files` capability is available |
| Peer container raises `ImportError` for a name in another agent's module | That agent's package `__init__.py` re-exports from its entrypoint, which is a stub in this image; V033 |
| `attempted relative import with no known parent package` | The entrypoint's own imports are relative, and it is loaded by path with no parent package; V035 |
| `ModuleNotFoundError` for a distribution this image's own code never imports | The entrypoint's package `__init__` or a sibling imports it; add it to this entry's `requirements:` |
| `SyntaxError` on the workflow's agent import | An `entrypoint` path segment is not a Python identifier; V034 |
| A model call fires at container start, before any request | Module-level code in the entrypoint performs a real run |
| `ModuleNotFoundError` for a submodule that used to exist | An unpinned requirement resolved to a newer major; pin it to what the source resolved |
| `unexpected keyword argument` inside an SDK call | The pinned distribution is newer than the source; pin contemporaneous with the source's commit date |

## Request is accepted, then fails

| Symptom | Likely cause |
|---|---|
| Unexpected keyword argument | yaml argument name differs from adapter parameter name |
| Required argument missing | yaml does not declare a required adapter parameter, or workflow platform sent only `query` |
| Unauthorized service | The first matching policy rule excludes that service |
| `.value()` returns dict-like text | Expected; remote values are strings, so use `json.loads` |
| Object is not JSON serializable | Adapter returned framework objects without its JSON-safe serializer |
| Redis contains a coroutine repr | Adapter method is async and the controller did not await it |
| Fan-out is no faster | Dispatch and `.value()` were fused, serializing the calls |
| Debug block runs at startup | Workflow is executed with `__name__ == "__main__"` |
| `Lock is bound to a different event loop` on the second request | `asyncio.run` per call, while the instance holds loop-bound state; use one persistent background loop |
| Model call reaches the real provider with the proxy configured | The SDK reads a base-URL variable the env file does not set; read [llm-proxy.md](llm-proxy.md) |

## Deployment platform endpoint

| Symptom | Likely cause |
|---|---|
| 404 while workflow container is healthy | Workflow function is not named `main` |
| 400 before host receives request | Body is not the platform's `{query: string}` shape |
| Extra workflow argument is missing | Platform sends only `query`; extra parameters need defaults |

## Cleanup

| Symptom | Likely cause |
|---|---|
| `ventis clean` succeeds but containers remain | The command removes generated directories only |
| `ventis clean` succeeds but images remain | Image deletion is separate and requires exact tags |
| Next deployment collides with old resources | Foreground deploy was killed or crashed before controller cleanup |
