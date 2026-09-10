# Prepare the `.car` artifact

**When:** before creating or refreshing `.car`, or when imports and runtime
assets need packaging decisions.

**Output:** a self-contained `.car/config` and `.car/app`, with the original
application tree untouched and existing imports preserved.

Use this order:

1. Read **Artifact model and preparation** and choose the import root from actual
   imports.
2. Run `prepare.py`; do not recreate guarantees already enforced by the script.
3. Use **Import roots, metadata, and runtime assets** only when direct `/app`
   imports are insufficient or the source opens non-Python files.
4. Use **Refresh an existing source copy** only when `.car/app` already exists.

## Artifact model and preparation

### Product and runtime names

CanyonOS Core is the product name and `canyonos` is its user-facing CLI. The
internal Python package, environment variables, and Docker resources are
named `canyonos_core`, `CANYONOS_*`, and `canyonos-*`. These are protocol
identifiers, not CLI instructions or branding strings. Do not rename them.

### Artifact boundary

The port lives entirely inside `.car/`, next to the application source:

```text
.car/config/global_controller.yaml   deployment manifest
.car/config/policy.yaml              optional access restriction
.car/config/<name>.yaml              one callable surface per service
.car/app/                            a copy of the application source
.car/app/<dir>/<name>.py             adapter beside the code it wraps
.car/app/<dir>/<name>_workflow.py    HTTP entry point; calls deploy()
.car/app/pyproject.toml              conditional nested-import scaffolding
<application source>/                untouched and unaware of the port
```

`.car` has exactly two authored directories: `config/`, which holds every
Canyon-owned declaration, and `app/`, which becomes `/app` in every container.
Nothing under `.car` points back into the original source, and nothing in the
original source points at `.car`. Deleting `.car` must restore the project to
its pre-port state.

Preserve the source's directory structure. Put adapters in the copied module
whose behavior they wrap unless the entrypoint rules require a sibling module;
do not invent generic `agents/` or `workflow/` directories. `canyonos` commands
run from the application root and read `.car` below it.

The file count follows the deployment: one yaml/adapter pair per independently
deployed service. If a copied source class already satisfies the runtime
contract, point its declaration at that class and do not add an adapter.

### Prepare the copy

Choose the source's **import root**, not automatically its repository root.
Without editable-install support, `/app` is the only source entry on
`sys.path`. For example, source under `src/` that says `from tools import ...`
needs the contents of `src/` copied directly into `.car/app/`. Decide from the
source's imports. Continue to **Import roots, metadata, and runtime assets** below when the
copy has those concerns.

Create the artifact with the skill script rather than ad hoc copy commands:

```bash
python3 <skill_dir>/prepare.py <import-root> .car
```

The script creates `.car/config/` and copies the import root's **contents** to
`.car/app/`. It excludes VCS data, `.car`, virtual environments, caches, build
outputs, bytecode, and credential-bearing `.env*` files while retaining
`.env.example`, `.env.sample`, and `.env.template`. It rejects symbolic links:
they can escape the artifact and may be skipped by runtime source sweeps.

If `.car/app` already exists, follow **Refresh an existing source copy** below. Use
`--force` only when every edit in `.car/app` may be discarded; it leaves
`.car/config/` unchanged.

After preparation, edit only `.car` and survey the copy using
[source-survey.md](source-survey.md). Do not add validation for preparation
postconditions that `prepare.py` already guarantees.

### Source-integrity boundary

The gap validator owns authored runtime contracts that CanyonOS tooling does
not strongly guarantee. The porter owns constraints static analysis cannot
prove:

- Never edit outside `.car`, or duplicate source-owned prompts, tools, schemas,
  model calls, parsing, retries, and node bodies in an adapter.
- Never swap providers, invent runtime configuration, or silently move, drop,
  or reclassify a dependency.
- Rewrite framework control flow only where it crosses a chosen service
  boundary; preserve it inside a service.
- Never hardcode or bake a real credential into `.car`.

When a source defect or unsupported runtime capability requires crossing one of
these boundaries, report the blocker and obtain approval for that specific
change. Do not broaden that approval to unrelated source edits.

## Import roots, metadata, and runtime assets

### What `/app` can import

CanyonOS Core copies `.car/app/` into the image with its paths intact and
starts Python at `/app`, so `/app` is that copy. Without an editable install,
Python resolves names rooted there:

- `/app/tools.py` as `import tools`
- `/app/pkg/__init__.py` as `import pkg`
- `/app/src/agents/...` as `import src.agents`, including PEP 420 namespace
  directories without `__init__.py`

It does not resolve `/app/source/pkg` as `import pkg`; `/app/source` must become
an import root first.

### Re-root the copy before reaching for metadata

`.car/app/` is a copy Canyon owns, so the cheapest fix is usually to root it
where the source already imports from. A project laid out as

```text
repo/src/email_assistant.py     imports `tools`, `prompts`, `utils`
repo/src/tools/
repo/pyproject.toml
```

has `src/` as its import root. Copy `src/`'s contents to `.car/app/` and every
one of those imports resolves from `/app` with no metadata, no editable install
and no `sys.path` hack. `entrypoint` and `workflow_file` then name modules
relative to that root, and the workflow imports the agent the same way.

Reach for the metadata below only when one copy root cannot serve every import
-- for instance when the source imports both `tools` and `src.tools`.

### Detect support, do not infer it from release history

Run:

```bash
python3 <skill_dir>/validate.py .car
```

Read the `editable_install` capability. If it is unavailable and the original
import cannot resolve from `/app`, report a runtime capability blocker and stop.
Do not add a `sys.path` hack or relocate source files.

### Root metadata is the trigger

When editable install is supported, only packaging metadata at the **root of
the copy** triggers `pip install -e .`:

```text
.car/app/pyproject.toml         detected
.car/app/source/pyproject.toml  ignored as an install trigger
```

If the application keeps its metadata deeper in the tree, that copy stays where
it is. Add minimal scaffolding at `.car/app/` that points package discovery at
the existing package:

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "canyonos-port"
version = "0.0.0"
dependencies = []

[tool.setuptools.packages.find]
where = ["source/src"]
include = ["pkg*"]
namespaces = true
```

Set `where` and `include` from the actual tree and original import spelling. Do
not reference a README or license from this wrapper metadata; file sweeps differ
by runtime capability and a missing referenced file makes the image build fail.

### Dependencies in nested metadata

A nested `pyproject.toml` is not installed merely because its Python files are
copied. Keep the source declaration unchanged and repeat its runtime
distributions in each relevant config entry's `requirements` list. This is
compatibility scaffolding, not permission to drop, move, or reclassify declared
dependencies.

If the application's own metadata already sits at the root of the copy, do not
create a wrapper. Its project dependencies participate in the same resolver as
config requirements.
Report declared-but-unused toolchain dependencies and their image cost; let the
owner decide whether source metadata should change.

### Runtime data and configuration files

`prepare.py` copies non-Python files into `.car/app`, but that does not prove the
runtime's image sweep carries them into a container. Inventory every file opened
by the selected import graph: prompt templates, JSON schemas, PDFs, local
corpora, certificates, and framework configuration such as CrewAI
`agents.yaml` and `tasks.yaml`.

Run `validate.py` and read its `sweeps_all_files` capability:

- When available, retain each asset at the same path relative to the chosen
  import root. Check any path derived from the original repository root or
  process working directory; the container starts from `/app`.
- When unavailable, a required non-Python asset is a runtime blocker. Report it
  and stop after validation. Do not conceal the gap by base64-encoding the file
  into Python, changing a hardcoded path, or duplicating framework config into
  adapter code; those changes restate source-owned data and behavior.

Do not treat successful construction as evidence that configuration loaded.
Frameworks such as CrewAI may warn about a missing yaml and create an empty
configuration, then fail only when the first agent or task is accessed. Inspect
those decorators and file references statically during the survey.

### Validation boundary

The build phase of `canyonos deploy` owns packaging syntax and installation
errors. `validate.py` checks only whether adapter imports appear to require a
nested root that the runtime will not expose.

## Refresh an existing source copy

Run the same preparation command with `--refresh` and the same import root:

```bash
python3 <skill_dir>/prepare.py <import-root> .car --refresh
```

The initial copy records source-file hashes in
`.car/config/.porting-state.json`. Refresh compares three states:

```text
previous source hash → current source
                    ↘ current .car/app
```

- Only the source changed: update `.car/app`.
- Only `.car/app` changed: preserve the port edit.
- The source added a path unused by the port: add it.
- The source deleted an unmodified path: delete it from `.car/app`.
- Both sides changed the same path differently: make no changes and report all
  conflicts.

Resolve a conflict in `.car/app`, then either make the source match that result
or intentionally start over. The script does not guess a merge because an
adapter and its source often change for different reasons while sharing one
module.

`--force` is not refresh. It discards the entire `.car/app` tree and replaces it
with a clean source copy while retaining `.car/config`. Use it only when every
adapter and workflow edit in `.car/app` is intentionally disposable.

After refresh, survey changed imports, dependencies, runtime assets, and service
boundaries again. Run gap validation for affected authored contracts, but do not
revalidate merge mechanics already guaranteed by the successful atomic refresh.
