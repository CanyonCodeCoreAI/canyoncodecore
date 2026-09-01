# CanyonOS Core runtime contract

The product is CanyonOS Core. Compatibility identifiers remain `ventis` for the
CLI and Python package, `VENTIS_*` for runtime variables, and `ventis-*` for
Docker resources.

Read this reference when implementing an adapter or explaining a validator
finding. Runtime-dependent behavior is expressed as capabilities; run
`validate.py` against the target environment instead of inferring support from
release history.

## Contents

- Artifact root and discovery
- Agent yaml and generated stubs
- Agent loading and execution
- Workflow execution
- Build context and collisions
- Dependencies and protobuf
- Credentials capability
- Policy and provider behavior
- Cleanup boundary

## Artifact root and discovery

`ventis build` runs from the application root and reads `.car` below it. That
artifact root holds `config/` beside `app/`, the copy of the application source
that becomes `/app` inside every image. Paths in the config are relative to
`app/` and may not escape it.

| Input | Discovery |
|---|---|
| agent declarations | any `.car/config/*.yaml` with a top-level `agent.name` |
| `.car/config/global_controller.yaml` | default config, overridable with `-c` |
| entrypoint | `entrypoint` on an agent entry, relative to `app/` |
| workflow | `workflow_file` on a `type: workflow` entry, relative to `app/` |
| policy | `policy.yaml` beside the selected config file |
| generated files | `stubs/`, `grpc_stubs/`, `docker_container/`, all under `.car` beside `app/` |

Because build products sit next to the copy rather than inside it, an
application directory named `stubs/` or `build/` survives into the image.

The config name, yaml `agent.name`, and entrypoint class name form one binding:

```text
config entry name == yaml agent.name == entrypoint class name
```

A missing match may skip an image while the command continues, so inspect build
output and generated image tags.

## Agent yaml and generated stubs

The consumed yaml shape is:

```yaml
agent:
  name: ExampleAgent
  functions:
    - name: work
      arguments:
        - name: query
          type: str
      returns:
        type: dict
```

Argument annotations are generated from bare names without adding imports. Use
builtins. Generated methods have no defaults, so every declared argument is
required at the stub call site. `returns` does not control runtime conversion;
it documents whether workflow code should parse the returned string.

A stub has exactly one destination: the agent's own `entrypoint` path. In every
image except that agent's own, the stub is written over the real module there,
so an import of the agent from its source location resolves to the stub and
travels over gRPC. The agent's own image keeps its real module and receives
only its peers' stubs. The validator checks workflow imports against those
entrypoints before the workflow image starts.

## Agent loading and execution

The local controller effectively performs:

```python
module = load(entrypoint)
agent_class = getattr(module, configured_name)
agent = agent_class()
result = getattr(agent, method_name)(**args)
```

Consequences:

- The class is module-level and named exactly as configured.
- Construction takes no arguments.
- Declared methods accept yaml argument names as keyword arguments.
- Methods are synchronous; this path does not await a coroutine.
- Dicts and lists are JSON-encoded before entering Redis; other results become
  strings.
- A remote Future's `.value()` returns text, not the original Python object.

Agent import and construction exceptions are caught by the controller. A failed
agent may still advertise healthy because health is written independently of
successful agent loading. That is why image probes import both the runtime and
the entrypoint explicitly.

## Workflow execution

The workflow file is executed, not imported. Therefore:

- module-level code runs at container startup;
- `__name__ == "__main__"`;
- `deploy()` blocks in the web server;
- the workflow function runs once per request;
- its function name determines the REST route exposed by the compatibility
  runtime.

The deployment platform additionally expects `/main` with a `{query: string}`
body. This platform constraint is stricter than the underlying transport.

Each stub method returns a Future immediately. `.value()` blocks. Dispatching
and resolving inside one comprehension serializes work without raising an
error; dispatch all calls first, then resolve them.

The workflow container also starts runtime controller code and has its own
package resolution. Probe it independently from agent images.

## Build context and collisions

The runtime sweeps `app/` while preserving relative paths, then writes shared
runtime modules, generated stubs, and entrypoints into the image. Later writes
can shadow swept files.

Avoid modules at the root of the copy named like runtime files, including:

```text
future.py
ventis_context.py
local_controller.py
local_controller_frontend.py
redis_client.py
grpc_options.py
bedrock.py
deploy.py
session_logging.py
workflow_launcher.py
```

Two agents also may not share one entrypoint: each stub is written over its own
entrypoint, so the second lands on the first and every caller reaches whichever
was built last. The validator checks both collisions.

File sweep and editable-install behavior are runtime capabilities. For nested
imports, follow [packaging.md](packaging.md).

## Dependencies and protobuf

Agent and workflow images include a small runtime dependency set. Config
`requirements` adds source-specific distributions. A malformed requirements
value can be normalized away while image generation continues; missing imports
then surface only when the agent loads.

The build compiles gRPC Python stubs on the host and copies them into images.
The image resolver does not necessarily know the generated-code version. A
source dependency that constrains protobuf below the host generator version can
produce a green image build that dies on:

```text
import local_controller
```

Always run that probe before probing the entrypoint. Treat a generated-code /
runtime-version mismatch as a CanyonOS Core runtime issue, not a reason to alter
source dependencies silently.

## Credentials capability

When `env_file` capability is available, the top-level config path is resolved
against the application root -- the directory the command runs from, not `.car`
-- and passed at container start. A `.env` beside the source stays out of the
artifacts. Hidden env files are not
copied into images. Invalid paths are deploy-preflight errors.

When the capability is unavailable, declaring `env_file` has no effect. If the
source needs credentials, report the capability blocker rather than hardcoding
or vendoring a secret.

A source that constructs its client at import time works only when credentials
are already in the container environment. Image entrypoint probes therefore use
the same env file as deployment.

For proxy-specific credential separation, read [llm-proxy.md](llm-proxy.md).

## Policy and provider behavior

No policy file means unrestricted service access. If a policy exists, it needs a
non-empty rules list. Rules are evaluated by specificity and first match;
services excluded from the selected rule fail after request acceptance.

Local provider handling is case-sensitive: use lowercase `local`. EC2 behavior
and remote networking are covered in [ec2.md](ec2.md).

## Cleanup boundary

Stopping foreground deploy normally invokes controller cleanup for recorded
containers and Redis. Hard kills and failures before resource registration may
leave resources behind.

`ventis clean` removes generated `stubs/`, `grpc_stubs/`, and
`docker_container/` under `.car`. It does not remove containers or images. Remove exact leftovers explicitly and preserve `app/`,
`config/`, and requested evidence.
