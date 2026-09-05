# Troubleshooting

Read this after a failed build, image probe, deploy, or request. For mechanisms,
read [runtime-contract.md](runtime-contract.md). For proxy or remote-host failures,
read [llm-proxy.md](llm-proxy.md) or [ec2.md](ec2.md).

## Build or deploy stops early

| Symptom | Likely cause |
|---|---|
| Agent image is missing | Config name matched no yaml name, entrypoint is absent, or build skipped it; inspect build warnings |
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
| Source module is missing | Original import does not resolve from `/app`; read [packaging.md](packaging.md) |
| Third-party module is missing | Distribution is absent from source metadata and config requirements |
| Stub import raises `NameError` | yaml argument type is not a bare builtin |
| Source module behaves like an empty stub | A generated stub basename shadowed the source module |
| Runtime-named project module disappears | Shared runtime copy overwrote a root project module with the same name |

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

## Deployment platform endpoint

| Symptom | Likely cause |
|---|---|
| 404 while workflow container is healthy | Workflow function is not named `main` |
| 400 before host receives request | Body is not the platform's `{query: string}` shape |
| Extra workflow argument is missing | Platform sends only `query`; extra parameters need defaults |

## Cleanup

| Symptom | Likely cause |
|---|---|
| `canyonos clean` succeeds but containers remain | The command removes generated directories only |
| `canyonos clean` succeeds but images remain | Image deletion is separate and requires exact tags |
| Next deployment collides with old resources | Foreground deploy was killed or crashed before controller cleanup |
