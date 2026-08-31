# Packaging and import roots

Read this reference when an adapter imports nested source code, the source uses a
`src/` layout, or V031 reports an import-root problem.

## What `/app` can import

CanyonOS Core preserves project-relative paths in the image and starts Python at
`/app`. Without an editable install, Python resolves names rooted there:

- `/app/tools.py` as `import tools`
- `/app/pkg/__init__.py` as `import pkg`
- `/app/src/agents/...` as `import src.agents`, including PEP 420 namespace
  directories without `__init__.py`

It does not resolve `/app/source/pkg` as `import pkg`; `/app/source` must become
an import root first.

## Detect support, do not infer it from release history

Run:

```bash
python <skill_dir>/validate.py .
```

Read the `editable_install` capability. If it is unavailable and the original
import cannot resolve from `/app`, report a runtime capability blocker and stop.
Do not add a `sys.path` hack or relocate source files.

## Root metadata is the trigger

When editable install is supported, only packaging metadata at the **port root**
triggers `pip install -e .`:

```text
port-root/pyproject.toml       detected
port-root/source/pyproject.toml ignored as an install trigger
```

A nested source repository may remain untouched. Add minimal root scaffolding
that points package discovery at the existing source package:

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

If source metadata is already at the port root, do not create a wrapper. Its
project dependencies participate in the same resolver as config requirements.
Report declared-but-unused toolchain dependencies and their image cost; let the
owner decide whether source metadata should change.

## Validation boundary

`ventis build` owns packaging syntax and installation errors. `validate.py`
checks only whether adapter imports appear to require a nested root that the
runtime will not expose.
