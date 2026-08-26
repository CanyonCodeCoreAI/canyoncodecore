# The Ventis contract

Every claim here should be read and validated from [https://github.com/CanyonCodeCoreAI/canyoncodecore](https://github.com/CanyonCodeCoreAI/canyoncodecore)

## Project layout


| Path                                        | Where                                                                                              |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `agents/*.yaml`                             | `cli.py` — `agents_dir`, then `glob(agents_dir/*.yaml)`                                            |
| `stubs/`, `grpc_stubs/`                     | `cli.py` — `stubs_dir`, `grpc_stubs_dir`                                                           |
| `config/global_controller.yaml`             | `cli.py` — `DEFAULT_CONFIG_PATH`, overridable with `--config`                                      |
| `config/policy.yaml`                        | `global_controller.py` `_load_policy_rules` — **required**, see below                              |
| workflow file                               | comes from the `workflow_file` key on the `type: workflow` config entry                            |
| the project root                            | `cli.py` passes `project_dir=os.getcwd()`; `cmd_build` must run from it                            |
| `pyproject.toml` / `setup.py` / `setup.cfg` | `generate_docker` — its presence is what adds `-e .`, and so what makes a `src/` layout importable |


## Agent yaml — the complete key set

```yaml
agent:
  name: <str>              # required
  functions:               # optional; absent -> stub class with only __init__
    - name: <str>          # required
      description: <str>   # optional -> becomes the stub method's docstring
      arguments:           # optional; absent -> no-arg method
        - name: <str>      # required
          type: <str>      # optional -> pasted verbatim as an annotation
      returns:
        type: <str>        # read, but affects nothing that gets generated
```

Nothing else is read. Extra keys are ignored silently.

### `type` is pasted, never checked

`_build_stub_method` does `ast.Name(id=arg["type"])`. The generated stub imports
only `Future` and `inspect`, so anything that isn't a builtin raises `NameError`
the moment the stub is imported.

Use: `str` `int` `float` `bool` `dict` `list`. Not `List[str]`, not `Optional[int]`,
not a class name.

### No default values

`_build_stub_method` builds `ast.arguments(..., defaults=[])`. Every argument you
declare is required at every call site. Optional configuration belongs in the
agent's `__init__`, read from the environment.

### Parameter names must match exactly

The controller invokes `method(**args)`. Order is irrelevant; spelling is not.
yaml `arguments[].name` must equal the Python parameter name character for
character, or the call raises `TypeError` at request time.

### `returns` is documentation

It changes nothing in the generated stub (whose return type is always `Future`).
Its value is as a marker: `type: dict` tells whoever writes the workflow that
this call site needs `json.loads`.

### The yaml filename is not part of the contract

It only names the generated stub file: `stubs/<yaml basename>.py`.

## The three-way name binding

```
config entry `name`  ==  agents/x.yaml `agent.name`  ==  the class inside the .py
        |
        `entrypoint` on that config entry points at the .py
```

`cmd_build` looks up each config entry's `name` among the parsed yamls. No match
means a logged warning and **no image built for that agent** — the build still
exits 0.

## Agent class


| Requirement                                     | Enforced by                                                                                                                          |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Class name equals `agent.name`                  | `generate_docker` writes `ENV VENTIS_AGENT_NAME=<agent.name>`; `LocalController._load_agent` does `getattr(module, self.agent_name)` |
| Instantiable with no arguments                  | `_load_agent` calls `agent_class()`                                                                                                  |
| Methods are synchronous                         | the executor calls `method(**args)` — there is no `await` anywhere on this path                                                      |
| Return values must survive `str()`/`json.dumps` | the executor does `json.dumps(result)` for `dict`/`list`, `str(result)` for everything else                                          |


`self.tools = [...]` appears throughout `examples/` and is read by **nothing** in
`ventis/`. It is decoration.

### `.value()` always returns a string

The result is written into Redis as text and handed back verbatim. There is no
deserialization on the way out. Every call whose yaml says `returns.type: dict`
needs `json.loads` at the call site.

## Workflow

The workflow file is **not imported — it is `exec`'d**. `generate_workflow_docker`
writes a `workflow_launcher.py` whose last line is
`exec(open("<workflow>.py").read())`, and the Dockerfile's CMD runs that launcher.

Consequences, all verified by running it:

- `__name__ == "__main__"` inside your workflow file. `if __name__ == "__main__":`
blocks **execute in production**.
- `__file__` points at `workflow_launcher.py`, not at your file. The
`sys.path.insert(..., "..", "stubs")` lines the examples carry resolve to
`/stubs` and `/grpc_stubs`, neither of which exists. Imports work anyway
because the stubs and the runtime are placed flat at `/app` and `sys.path[0]`
is `/app`. Those lines matter only when running the workflow directly from a
nested source tree. The *project* tree keeps its own paths, so what makes
`src/` importable is the editable install, not `sys.path[0]`.
- `deploy()` ends in `app.run()` and blocks. Nothing after it runs.
- Module-level code runs **once** at container start. `main()` runs **per
request**, on a Flask worker thread.
- The REST route is `main.__name__` — rename the function and the endpoint
renames with it. There is no fixed `/main`.
- The request body is splatted into `main()` as kwargs after `_context` is
popped off.

The workflow container also runs its own `LocalController` on 50051 in a
background thread. That is what dispatches the Futures the workflow creates.

## `config/policy.yaml` is required, not optional

`_load_policy_rules` logs `No policy file found ..., skipping policy setup` and
bare-`return`s, so it hands back `None`. `_load_and_write_policies` then does
`json.dumps(rules)` and `len(rules)` on it. `ventis deploy` dies in
`GlobalController.__init__`, before any container is launched:

```
INFO  No policy file found at .../config/policy.yaml, skipping policy setup.
TypeError: object of type 'NoneType' has no len()
```

Reproduced on a port that had no policy file. Every example under `examples/`
that predates this ships one, which is why the path had never run. A rule with
an empty `match` is the default, and any service missing from its `access` list
answers `Unauthorized: Policy denied access to service 'X'` in `/status` —
`examples/finance` returns exactly that for `VllmAgent`.

## `provider` is case-sensitive in one direction only

`InstanceManager.launch_all` reads `agent_spec.get("provider", "local")` and
tests `provider == "local"` to decide whether to reserve a host port. `Local`
fails that test, `reserved_port` stays `None`, and `Local/_runtime.py`'s
`int(spec.get("host_port", spec.get("port", next_host_port(host))))` raises
`int() argument must be a string, a bytes-like object or a real number, not
'NoneType'`. The EC2 test on the same value is `.upper() == "EC2"` in both
`cli.py` and `global_controller.py`, so it accepts any casing. Every example
that works writes lowercase `local`.

## Failures are silent

`_load_agent` catches every exception, logs it, and returns `None`.


| Stage           | A missing credential / a missing dependency / a wrong class name |
| --------------- | ---------------------------------------------------------------- |
| `ventis build`  | passes — it never imports your agent                             |
| `ventis deploy` | passes — the container starts, gRPC listens                      |
| first request   | `"No agent loaded"`                                              |


The real cause exists only in that container's stdout. Plan for this: `ventis build`
succeeding tells you almost nothing about whether the project works.

Worse, the node still advertises itself as usable. `LocalController.__init__`
writes `healthy` to `controller:<host>:<port>:status` **before** calling
`_load_agent`, and `_metrics_loop` re-writes `healthy` on every tick. Nothing
downgrades the status when the agent fails to load. Verified end to end on a
real port: `ventis build` printed `Build complete.`, both images were tagged,
the container came up, logged one `ERROR ... No module named 'open_deep_research'`
line, and Redis reported `controller:localhost:50051:status = healthy`.

## The build context

`generate_docker` takes a `project_dir` and `cmd_build` passes it, so the whole
project reaches the image with its relative paths intact. Structure is preserved
rather than flattened because packages need it: `src/tools/__init__.py` and
`src/tools/default/__init__.py` flatten to the same name.


Copy order decides every collision below: the swept tree first, then the shared
runtime, then every stub, then the entrypoint. Later writes land on earlier ones.

| What lands where     |                                                                                                                                                          |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the project tree     | at its own relative paths (`agents/x.py`, `src/pkg/mod.py`)                                                                                              |
| the shared runtime   | flat at the context root, and it wins over the swept tree — `local_controller.py` is the CMD, so a project file of that name breaks the container        |
| every stub           | **twice**: flat at the root, and at `agents/<yaml basename>.py`. Flat is the one imports resolve; the `agents/` copy lands on the real implementation the sweep put there, so a peer's name gives the caller its stub |
| the entrypoint       | flat, last, so it wins the flat name back — `python local_controller.py` loads `VENTIS_AGENT_FILE`, which is a **basename**, from `/app`                  |
| `requirements.txt`   | written before anything is copied, so the sweep skips a project's own `requirements.txt` and `Dockerfile` at the root rather than letting them overwrite  |

The sweep takes every file, not only `.py` — the editable install below reads
packaging metadata, and that metadata points at a README or a license. It skips
hidden files and directories (`.env` holds credentials and the context is what
ships; `.env.example` goes with them), `__pycache__`, and the three directories
`ventis build` generates: `docker_container` (the context lives inside it, so
copying recurses), `stubs`, `grpc_stubs`.

### Two walls that were removed

An agent is no longer one file, and a yaml sharing the entrypoint's basename no
longer eats its own stub. Verified by building `examples/helloworld`, whose
`agents/example_agent.yaml` and `agents/example_agent.py` are exactly that case,
and running the image: `/app/example_agent.py` is the implementation (the
entrypoint wrote last), `/app/agents/example_agent.py` is the stub, the agent
loads, and its peer's stub still imports as `vllm_agent.VllmAgent`.

What an agent loses is the ability to import *its own* stub by name — the
entrypoint shadows it flat. It can still reach it at `agents/<yaml basename>.py`,
and nothing in `examples/` wants to.

### The import root

The Dockerfile adds `RUN uv pip install --system -r requirements.txt -e .` when
the project root has a `pyproject.toml`, `setup.py` or `setup.cfg`. That editable
install is what makes a `src/` layout importable, and the source's own packaging
metadata is what decides it — `[tool.setuptools.package-dir] "" = "src"` in
email_assistant's case. Ventis never guesses a directory name.

Without packaging metadata the install is skipped — silently, there is no
warning. The tree is still copied, but `sys.path[0]` is `/app`, so only modules
that landed flat resolve. `examples/helloworld`, `finance` and `text2sql` are all
in this state; they work because their entrypoints import nothing from the
project tree at all — only stubs, which land flat.

**One resolve, not two.** Both are passed to a single `uv pip install` so the
runtime's list and the source's own dependencies resolve against each other. A
genuine conflict then fails the build instead of the first request. It also
forces `COPY . .` ahead of the install — the project has to be in the context
before it can be installed — so the requirements layer no longer caches on its
own.

## The credential wall

`_launch_locally` builds its `docker run` with exactly five `-e` flags, all
`VENTIS_*`: `VENTIS_AGENT_PORT`, `VENTIS_AGENT_HOST`, `VENTIS_REDIS_HOST`,
`VENTIS_REDIS_PORT`, `VENTIS_POLL_INTERVAL`. A workflow entry adds
`VENTIS_DATABASE_URL` and `VENTIS_PROJECT_ID` when configured. **There is no
mechanism of any kind for passing a secret to an agent container**, and `.env` is
excluded from the build context by design.

So any source that constructs its model client at module scope cannot load.
Reproduced on the email_assistant image with no key in the environment — this is
`_load_agent`'s own log line, and `VENTIS_AGENT_FILE` is a basename, so the path
it prints is the flat copy:

```
ERROR Failed to load agent EmailAssistant from /app/email_agent.py:
      Missing credentials. Please pass an `api_key` ... or set the OPENAI_API_KEY
      ... environment variable.
```

Passing `-e OPENAI_API_KEY=...` by hand to `docker run` loads it. Nothing in
`ventis deploy` can do that.

`load_dotenv(".env")` does not help — the file is not in the image and
`load_dotenv` is silent about a missing one. This is the most common cause of
`"No agent loaded"`, and unblocking it needs an `env:` key on the config entry or
pass-through of named host variables. Neither exists.

## Dependencies: a wall that was removed

They used to be a third one: `generate_docker` wrote a fixed requirements.txt
with no config hook, so `langchain` and `langgraph` could not be installed into
an agent image at all. That is no longer true.

`generate_docker` and `generate_workflow_docker` both take a `requirements`
argument, and `cmd_build` passes `_normalize_requirements(agent_cfg)` to each.
The runtime's own list is still unconditional and still not declarable —

```
agent:     grpcio grpcio-tools redis pyyaml psutil ipdb ipython boto3
workflow:  the same, plus flask sqlalchemy psycopg[binary]
```

— and the declared list is appended to it verbatim, in `BASE_AGENT_REQUIREMENTS`
and `BASE_WORKFLOW_REQUIREMENTS`. Note what is no longer in there: `yfinance`
used to go into every image and does not any more, so a source that imports it
now has to say so.

### A third wall, still open: the gRPC stack is unpinned

`cmd_build` runs `grpc_tools.protoc` on the **host** and copies the resulting
`_pb2.py` into the image, where a resolver that knows nothing about them picks
the protobuf runtime. Protobuf refuses to load gencode newer than its runtime, so
a source whose own dependencies drag protobuf down produces a container that dies
on `import local_controller` — before the agent is reached, with a green build
behind it.

Reproduced on the email_assistant image, which pulls langchain and langgraph:

```
$ docker run --rm ventis-emailassistant python -c "import local_controller"
google.protobuf.runtime_version.VersionError: Detected incompatible Protobuf
Gencode/Runtime versions when loading local_controler.proto:
gencode 7.35.1 runtime 6.33.6.
```

`ventis-academiccoordinator` and `ventis-exampleagent` both resolve protobuf
7.36.0 and import fine — so an image with few requirements works by coincidence,
resolving to the newest wheel, which happens to be at least as new as the host's.

Nothing pins this. A fix means prepending `grpcio==`, `grpcio-tools==` and
`protobuf>=` at the host's own versions (`importlib.metadata.version`) to the
generated requirements, with `>=` on protobuf because the guarantee runs one way:
a runtime at or above the gencode.

**This is the wall to check first on any port that installs a large dependency
tree.** Probing the entrypoint module is not enough — it does not import
`local_controller`, which is what the container's CMD actually runs.

`_normalize_requirements` takes only a list of strings. A bare string, a mapping,
a list with a non-string in it — each logs one warning naming the agent and
becomes `[]`, so a malformed entry costs the whole list rather than the one item.
Nothing is deduplicated against the base either: declaring `redis` writes a
second pin and leaves pip to resolve the two.

**The source's own `pyproject.toml` is now installed**, in the same resolve, so
`requirements:` covers only what the adapter imports and the source does not
depend on. The whole dependency list comes along, dev extras included:
email_assistant's brings jupyter, matplotlib, pandas and pyppeteer, and the agent
image is 1.1GB. When `requirements:` does drift, the failure is the silent one
above: green build, `healthy` replica, `"No agent loaded"` on the first request.