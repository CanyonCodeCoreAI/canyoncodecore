# Writing what goes into `.car/config`

Read this before writing the manifest or an agent declaration. `ventis build`
owns yaml syntax; nothing here is syntax. These are the values that build green
and then decide whether a container can import its own dependencies.

## Contents

- Who decides each key
- Agent yaml
- Requirements
- The manifest, in full

## Who decides each key

Two kinds of key share one file. A **derived** key has exactly one right answer
and the copy holds it; asking the developer can only make it worse. A
**developer** key is a deployment choice the source does not contain, and
deriving it means guessing and presenting the guess as a reading.

SKILL.md step 3 shows the whole manifest and then asks about the second column
only, in one round, carrying these defaults.

| Key | Decided by | Default when unanswered |
|---|---|---|
| `name`, `entrypoint`, `workflow_file`, `type` | derived — service boundaries, step 2 | — |
| `requirements` | derived — the entry's import graph | — |
| `database` | neither; omit it always (see below) | absent |
| `provider` | developer | `local` |
| `ec2:` block, `instance_type` | developer — no default is safe | entry stays `local` |
| `replicas` | developer, *unless* cross-request state forces `1` | `1` |
| `resources.cpu` / `resources.memory` | developer | `1` / `512` MiB |
| `api_port` | developer | `8080` |
| `redis_port`, `redis.host` / `.port` / `.db` | developer | `6379`, `localhost` / `6379` / `0` |
| `poll_interval` | developer | `5` |
| `env_file` | developer — the file's location and whether it exists | `.env` when the survey found credential reads, else absent |
| `policy.yaml` | developer | absent |

Two entries in that table are not free choices, and saying so is part of showing
the config rather than asking about it:

- **`replicas` stops being a choice once a service holds cross-request state.**
  Where the step-2 survey found such state, SKILL.md already fixes `replicas: 1`
  as a correctness requirement, so `1` is derived: report it as a constraint and
  do not offer to raise it.
- **EC2 identifiers are wrong to invent.** ec2.md forbids copying them from an
  example environment, and a wrong AMI, subnet, or security group fails at
  deploy preflight or, worse, provisions something unreachable. Unanswered
  means the entry stays `local`.

## Agent yaml

Declarations go in `.car/config/`, beside the manifest. The build reads every
`*.yaml` there and keeps the ones with a top-level `agent.name`, so the
manifest and `policy.yaml` drop out on their own.

Use one yaml per deployed service. Argument types are bare builtins only:
`str`, `int`, `float`, `bool`, `dict`, or `list`. Every declared argument is
required by the generated stub. `returns.type` is documentation; use `dict` or
`list` to signal that workflow callers must `json.loads` the returned string.

## Requirements

Each image installs the runtime's base list plus that entry's `requirements:`
and nothing else. The source's own `requirements.txt` is never installed -- the
generator writes its own -- and its `pyproject.toml` is installed only where the
editable-install capability is available. Re-declare every runtime distribution
by hand, per entry.

Build each entry's list from the imports its image *executes*, not from the code
you wrote:

1. Follow module-scope imports out of the entrypoint (or `workflow_file`) into
   the copy, transitively. A workflow that imports `benchmark.py`, which imports
   `agent.py`, needs `agent.py`'s distributions even though the workflow makes
   no model call.
2. Include the `__init__.py` of every package on those paths -- it runs first.
   In a peer image the entrypoint is a stub, but its package `__init__` and its
   siblings are real, so that image still installs what they import.
3. Omit distributions reachable only from source files no image imports, such as
   a Gradio or Streamlit UI beside the agent. M22 forbids reclassifying a
   declared dependency, not declining to ship an unreachable one; name what you
   left out in the report.

`validate.py` walks the same graph and reports what is missing as W006.

Version them the way the source resolved them, not the way PyPI resolves them
today:

- **The source has a lockfile** (`poetry.lock`, `uv.lock`, a pinned
  `requirements.txt`): copy those exact versions. Repeating the bare names
  resolved `langchain` 1.x for one port, which no longer has
  `langchain.agents.agent_toolkits` -- the untouched source's own import.
- **The source pins nothing**: cap every fast-moving distribution below its next
  major (`langchain<1.0`, `openai<2`). Unpinned means "whatever existed when
  this was written", which is not what pip installs today.
- **The source predates a known SDK break**: pin contemporaneous with its last
  commit. A 2023 AutoGen script passing `request_timeout=` needs
  `pyautogen==0.1.14`, which depends on `openai<1`, not `autogen==0.7.5`, which
  floors on `openai>=1.58` where that kwarg is `timeout`. M23 forbids rewriting
  the source call, so the pin has to absorb the difference. Compare the source's
  commit date against the pin's release date whenever the source hardcodes SDK
  kwargs.

Resolve the list before writing any adapter -- `uv pip compile`, or
`pip install --dry-run -r` into a scratch environment. A source whose own locked
graph is no longer installable (a yanked release series that an unconditional
transitive pin still requires) is a port blocker; one command finds it instead
of one build-fail/pin/rebuild cycle per attempt. Report it and stop rather than
upgrading the source out of the problem.

## The manifest, in full

`.car/config/global_controller.yaml` in full -- every key the runtime reads,
and no others:

```yaml
agents:
  - name: EmailAgent            # == yaml agent.name == entrypoint class name
    entrypoint: email_assistant.py   # relative to .car/app, may not escape it
    provider: local             # lowercase; `Local` fails an equality test
    replicas: 1                 # integer
    redis_port: 6379            # host port for this node's Redis; default 6379
    resources:                  # optional; defaults are cpu 1, memory 512
      cpu: 1
      memory: 1024              # MiB
    requirements:               # see Requirements above
      - langgraph
      - langchain-openai

  - name: Workflow
    type: workflow              # the one entry that carries this key
    workflow_file: email_workflow.py   # relative to .car/app
    api_port: 8080              # where /main is served
    provider: local
    replicas: 1
    redis_port: 6379
    requirements:               # its own list; the agent's does not apply here
      - langgraph

poll_interval: 5                # seconds between metrics polls; default 5

redis:
  host: localhost
  port: 6379
  db: 0

env_file: .env                  # relative to the application root, not .car
```

Omit `database`. Without it every metrics poll logs `Could not parse SQLAlchemy
URL from given URL string`, once per replica every `poll_interval` seconds --
expected noise, not a failure, and not a reason to add the key. Adding it drops
a sqlite file at the application root, outside `.car`, which the step-8
`git status` check then fails on.
