# Packaging and import roots

Read this reference when an adapter imports nested source code, the source uses a
`src/` layout, V031 reports an import-root problem, or the source reads
non-Python files at runtime.

## Contents

- What `/app` can import
- Re-root the copy before reaching for metadata
- Detect support, do not infer it from release history
- Root metadata is the trigger
- Dependencies in nested metadata
- Runtime data and configuration files
- Validation boundary

## What `/app` can import

CanyonOS Core copies `.car/app/` into the image with its paths intact and
starts Python at `/app`, so `/app` is that copy. Without an editable install,
Python resolves names rooted there:

- `/app/tools.py` as `import tools`
- `/app/pkg/__init__.py` as `import pkg`
- `/app/src/agents/...` as `import src.agents`, including PEP 420 namespace
  directories without `__init__.py`

It does not resolve `/app/source/pkg` as `import pkg`; `/app/source` must become
an import root first.

## Re-root the copy before reaching for metadata

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

## Detect support, do not infer it from release history

Run:

```bash
python <skill_dir>/validate.py .car
```

Read the `editable_install` capability. If it is unavailable and the original
import cannot resolve from `/app`, report a runtime capability blocker and stop.
Do not add a `sys.path` hack or relocate source files.

## Root metadata is the trigger

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

## Dependencies in nested metadata

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

## Runtime data and configuration files

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

## Validation boundary

The build phase of `canyonos deploy` owns packaging syntax and installation
errors. `validate.py` checks only whether adapter imports appear to require a
nested root that the runtime will not expose.
