# The Ventis contract

Every claim here is read from `ventis/`. Line numbers drift — re-grep the symbol
before relying on a number. `examples/` is **not** a reference: `examples/portfolio/`
carries committed merge-conflict markers, and `examples/helloworld/workflow/`
imports `ExampleAgentStub`, a name the generator no longer produces.

## Project layout

| Path | Fixed? | Where |
|---|---|---|
| `agents/*.yaml` | hardcoded | `cli.py` — `agents_dir`, then `glob(agents_dir/*.yaml)` |
| `stubs/`, `grpc_stubs/` | hardcoded | `cli.py` — `stubs_dir`, `grpc_stubs_dir` |
| `config/global_controller.yaml` | default only | `cli.py` — `DEFAULT_CONFIG_PATH`, overridable with `--config` |
| `config/policy.yaml` | optional | `global_controller.py` `_load_policy_rules` — absent file is skipped |
| workflow file | **not fixed** | comes from the `workflow_file` key on the `type: workflow` config entry |

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

| Requirement | Enforced by |
|---|---|
| Class name equals `agent.name` | `generate_docker` writes `ENV VENTIS_AGENT_NAME=<agent.name>`; `LocalController._load_agent` does `getattr(module, self.agent_name)` |
| Instantiable with no arguments | `_load_agent` calls `agent_class()` |
| Methods are synchronous | the executor calls `method(**args)` — there is no `await` anywhere on this path |
| Return values must survive `str()`/`json.dumps` | the executor does `json.dumps(result)` for `dict`/`list`, `str(result)` for everything else |

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
  because the container flattens every file into `/app` and `sys.path[0]` is
  `/app`. Those lines matter only when running the workflow directly from a
  nested source tree.
- `deploy()` ends in `app.run()` and blocks. Nothing after it runs.
- Module-level code runs **once** at container start. `main()` runs **per
  request**, on a Flask worker thread.
- The REST route is `main.__name__` — rename the function and the endpoint
  renames with it. There is no fixed `/main`.
- The request body is splatted into `main()` as kwargs after `_context` is
  popped off.

The workflow container also runs its own `LocalController` on 50051 in a
background thread. That is what dispatches the Futures the workflow creates.

## Failures are silent

`_load_agent` catches every exception, logs it, and returns `None`.

| Stage | A missing dependency / missing sibling module / wrong class name |
|---|---|
| `ventis build` | passes — it never imports your agent |
| `ventis deploy` | passes — the container starts, gRPC listens |
| first request | `"No agent loaded"` |

The real cause exists only in that container's stdout. Plan for this: `ventis build`
succeeding tells you almost nothing about whether the project works.

Worse, the node still advertises itself as usable. `LocalController.__init__`
writes `healthy` to `controller:<host>:<port>:status` **before** calling
`_load_agent`, and `_metrics_loop` re-writes `healthy` on every tick. Nothing
downgrades the status when the agent fails to load. Verified end to end on a
real port: `ventis build` printed `Build complete.`, both images were tagged,
the container came up, logged one `ERROR ... No module named 'open_deep_research'`
line, and Redis reported `controller:localhost:50051:status = healthy`.

## Two known walls

Both confirmed by running `generate_docker` on a minimal agent and listing the
output directory.

**An agent is exactly one file.** `generate_docker`'s `files_to_copy` takes the
`entrypoint` file and nothing next to it. A sibling `utils.py` never reaches the
build context.

**The stub and the implementation can collide.** The generated stub is named
after the yaml (`stubs/<yaml basename>.py`) and the Docker context is flat, so
`agents/x.yaml` plus `agents/x.py` both land on `/app/x.py`. `files_to_copy`
appends the stubs first and the entrypoint last, so the implementation wins and
the agent's own stub is gone — no warning, no error. Observed on a real port
where the yaml and the entrypoint shared a basename, which is exactly what
`examples/helloworld` does.

Neither is fixed. When a port needs one, stop and report it — do not paper over
it by inlining thousands of lines.

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

`_normalize_requirements` takes only a list of strings. A bare string, a mapping,
a list with a non-string in it — each logs one warning naming the agent and
becomes `[]`, so a malformed entry costs the whole list rather than the one item.
Nothing is deduplicated against the base either: declaring `redis` writes a
second pin and leaves pip to resolve the two.

Nothing resolves that list against the source's own `pyproject.toml`. It is
written into the image verbatim, so keeping it in step with what the source
imports is the port's job. When it drifts, the failure is the silent one above:
green build, `healthy` replica, `"No agent loaded"` on the first request.
