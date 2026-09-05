# Ventis → CanyonOS Rename: Verified Migration Plan

> **Audit status (2026-09-05):** re-checked against the complete tracked tree,
> hidden files, ignored/generated state, current tests, and Canyon Code company
> memory. Seven independent Luna agents audited runtime, packaging, infrastructure,
> tests, documentation, edge cases, and inventory. This file is analysis and an
> execution plan only; no runtime code has been renamed yet.

## Executive verdict

Do **not** implement this as a global search-and-replace. The current tree has one
blocking architecture decision and several versioned contracts that require an
additive compatibility phase.

1. **Blocking package-name collision.** The root distribution/package is currently
   `ventis` (`pyproject.toml:2,24,31`), while `cli/` already owns the distribution,
   import package, and executable name `canyonos` (`cli/pyproject.toml:2,14,21`).
   Renaming the root package and distribution to `canyonos` would make two editable
   projects provide the same distribution and top-level package. A temporary
   reproduction fails `uv lock --offline` with conflicting URLs for `canyonos`.
2. **The repository deliberately documents old names as compatibility protocol.**
   `.claude/skills/porting-to-canyonos-core/SKILL.md:4-12` and
   `references/runtime-contract.md:1-5` currently require the `ventis` Python/CLI,
   `VENTIS_*` variables, and `ventis-*` Docker names. That policy must be replaced
   or deprecated; simply changing code makes the skill teach users the wrong API.
3. **Generated artifacts are part of the runtime contract.** The build generator
   creates a nested `ventis.llm_proxy` package, copies `ventis_context.py`, writes
   `VENTIS_AGENT_*`, and the generated controller launches `python -m
   ventis.llm_proxy`. Updating only the source package will produce images that
   build but fail at runtime.
4. **The host CLI participates in the old protocol.** Contrary to the previous
   draft, `cli/canyonos/init.py:156` writes `VENTIS_REDIS_HOST` into the Global
   Controller container. It also consumes old Docker prefixes and the validator's
   `capabilities.ventis` JSON field.
5. **The test baseline is not green.** A clean `uv run pytest -q` currently stops
   with nine collection errors because `local_controler_pb2` is not generated.
   With both protos generated into a temporary `PYTHONPATH`, the baseline is
   **212 passed, 11 failed, 3 subtests passed**. Those 11 failures are pre-existing
   behavior/test drift, not rename regressions.

The safe route is: decide package ownership, introduce CanyonOS names alongside
legacy readers/aliases, switch every producer and consumer together, validate
fresh and upgrade deployments, then remove compatibility names in a later major
release. A literal zero-match tree is the **end state**, not a safe first commit.

> **NOTE:** The operator has since chosen a **hard cutover with no compatibility
> shims** (see "Locked decisions" below). That decision **supersedes** every
> "additive/compat/fallback/dual-read/shim" recommendation in this document. The
> *contract coupling* (which producers and consumers must change together) still
> fully applies — only the transitional fallbacks are dropped. Where a section
> below proposes old+new fallbacks, read it as "change producer and consumer to the
> new name in the same commit; delete the old name outright."

## Locked decisions (2026-09-05, operator-approved)

Hard cutover. **No** legacy env-var dual-read, **no** legacy import alias, **no**
old-header fallback, **no** compatibility release window. Existing running
deployments must be torn down and rebuilt; this is an accepted breaking change.

| Surface | Decision |
|---|---|
| Core import package / dir | `canyonos_core` (dir `ventis/` → `canyonos_core/`); all imports `from canyonos_core…` |
| Core distribution name | `canyonos-core` |
| `ventis_context` module + alias | `canyonos_context.py` / alias `canyonos_context` |
| Env vars | `VENTIS_*` → `CANYONOS_*` (⚠ collision note below) |
| Resource prefix (network/image/container/redis/tags/ids) | `canyonos-*` (`canyonos-local`, `canyonos-<agent>`, `canyonos-redis-*`, `canyonos-ec2-*`) |
| Future-ID HTTP header | `X-Canyonos-Future-ID` (`Canyonos` cased, `ID` all-caps) — injector **and** reader standardized to this exact spelling |
| Published GC image | **unchanged** — stays `saakeths/canyonos:latest` |
| Core executable | **removed** — core is import-only; the standalone `canyonos` CLI is the sole console script |
| Root `pyproject.toml` | **NOT deleted — rewritten import-only** (deleting breaks the container build; see below) |
| `uv.lock` | regenerate **after** the rename + pyproject rewrite land; never hand-edit |
| SSH key default | **unchanged** — stays `~/.ssh/ventis_ec2` for now |
| Logo asset + README clone URL | **unchanged** for now (`images/ventis-logo.png`, git URL) |

**Root `pyproject.toml` is critical — do not delete.** `ventis/Dockerfile:5-6`
runs `COPY . /ventis` + `RUN pip install /ventis`, which requires the root
`pyproject.toml`. It also supplies the entire runtime dependency set (boto3,
grpcio(-tools), redis, sqlalchemy, psycopg, flask, opentelemetry-\*) and the
`[tool.setuptools.package-data]` that ships `controller/proto/*.proto` and
`controller/utils/aws_pricing_chart.db` into the installed package. Deleting it
makes the container build fail and the runtime lose its bundled data. **Rewrite it
instead:**
- `name = "canyonos-core"`, `version` kept;
- **drop** `[project.scripts]` entirely (import-only);
- **drop** `[dependency-groups] dev` `canyonos` entry **and** `[tool.uv.sources]
  canyonos = { path = "cli", editable = true }` — this self-dependency is exactly
  what caused the `uv lock` collision noted in company memory; removing it is what
  makes the two distributions coexist;
- `[tool.setuptools.packages.find] include = ["canyonos_core*"]`;
- `[tool.setuptools.package-data]` key `ventis` → `canyonos_core` (and drop the
  stale `templates/**/*` entry — that dir no longer exists);
- Ty `[tool.ty.*]` include/exclude/allowed-unresolved-import paths
  (`ventis` → `canyonos_core`, `ventis_context` → `canyonos_context`).

**⚠ `CANYONOS_*` collision watch.** `VENTIS_REDIS_HOST`/`VENTIS_REDIS_PORT` become
`CANYONOS_REDIS_HOST`/`CANYONOS_REDIS_PORT`, which are **also** the names the
user-side dashboard stack already writes (`cli/canyonos/dashboard_stack.py:227-228`).
They live in different process/compose scopes (core GC/agent containers vs. the
dashboard compose), so there is no runtime clash today — but the names are now
semantically overloaded. Verify no single process reads both; if that ever changes,
the core vars would need a `CANYONOS_CORE_*` namespace.

## Verified inventory

The canonical count uses the tracked `HEAD` tree (before this currently untracked
analysis file is added) and case-insensitive token matches. All future recounts
must exclude this document so it does not count its own inventory:

| Scope | Matching files | Occurrences | Matching lines |
|---|---:|---:|---:|
| Source tree, excluding this document and `uv.lock` | 93 | 628 | 578 |
| `uv.lock` | 1 | 1 | 1 |
| Source tree including `uv.lock`, excluding this document | 94 | 629 | 579 |

Exact-case totals excluding `uv.lock` are 458 `ventis`, 62 `Ventis`, and 108
`VENTIS`. There are 173 tracked files total. The 94 matching files break down as:

| Area | Files with content matches |
|---|---:|
| Root runtime directory | 36 |
| Tests | 25 |
| Examples | 15 |
| Vendored porting skill | 7 |
| Standalone CLI | 7 |
| Root metadata/docs | 4 |

There are 55 tracked paths containing the old name: all 53 files under `ventis/`,
plus `images/ventis-logo.png` and `tests/test_ventis_context.py`. The migration
document's own filename/content must be excluded while work is in progress and
renamed or archived at final cleanup.

Reproduce the audit with:

```bash
git grep -I -i -l ventis -- . ':!uv.lock' ':!VENTIS_TO_CANYONOS_RENAME.md' | wc -l
git grep -I -i -o ventis -- . ':!uv.lock' ':!VENTIS_TO_CANYONOS_RENAME.md' | wc -l
git grep -I -i -n ventis -- . ':!VENTIS_TO_CANYONOS_RENAME.md'
git ls-files | rg -i 'ventis'
find . -path './.git' -prune -o -iname '*ventis*' -print
rg --hidden --no-ignore -i ventis -g '!.git/**'
```

`git grep` is the tracked-source authority; the final `rg` catches stale virtual
environments, editable-install metadata, generated `.car/` output, and other
ignored files that can mask a bad migration.

## Decision gate 1: package and executable ownership

This must be resolved before moving `ventis/`. **RESOLVED** — see Locked decisions.

### Topology (locked)

| Surface | Owner/name |
|---|---|
| User-facing distribution | existing `cli/` distribution: `canyonos` |
| User-facing import package | existing `cli/canyonos/` |
| User-facing executable | existing `canyonos` console script (the **only** one) |
| Core runtime distribution | `canyonos-core` |
| Core runtime import package | `canyonos_core` |
| In-container entrypoints | `python -m canyonos_core.server`, `.cli`, `.llm_proxy` |
| Legacy package/executable | **none** — hard cutover, no shim; core console script removed |

This keeps the already-thin host CLI independent and avoids two wheels overwriting
one `canyonos/` directory. It also lets the root runtime be versioned independently
inside `saakeths/canyonos:<version>`.

The valid alternative is to merge the root runtime into the existing CLI
distribution under one intentionally owned `canyonos` tree. That is a larger
packaging refactor and must include dependency, image, and release ownership.

**Invalid topology:** changing root `name`, package include, directory, and console
script to `canyonos` while leaving `cli/pyproject.toml` unchanged. It breaks lock
resolution, editable installs, module resolution from the repo root, and script
ownership.

Also decide whether the old root command remains temporarily available. The old
runtime command exposes `new-project`, `deploy`, and `clean`; the standalone
`canyonos` CLI exposes `new-app`, agent-driven `build`, HTTP-driven `deploy`, and
other commands. `tests/run_tests.sh` therefore cannot be fixed by replacing the
word in place—the desired command semantics must be mapped explicitly.

## Decision gate 2: compatibility policy

**RESOLVED — hard cutover, declared breaking.** No compatibility window, no
dual-read, no fallback. A full teardown/rebuild is required; existing deployments
do **not** survive the switch. Every producer and its consumer(s) change to the new
name in the same commit, and the old name is deleted outright. The per-item
"legacy fallback" bullets below are **void** and retained only to enumerate the
producer/consumer pairs that must move together:

- Env variable: producer + consumer switch to `CANYONOS_*` together; no `VENTIS_*` reader remains.
- HTTP header: injector + reader switch to `X-Canyonos-Future-ID` together.
- Validator capability key: emitter (`validate.py`) + consumer (`cli/canyonos/verify.py`) + tests switch together.
- SSH default: **unchanged** (`~/.ssh/ventis_ec2`) per Locked decisions.
- Docker/resource prefixes: generators + `cli/canyonos/verify.py` switch to `canyonos-*` together; no old-prefix recognition.

## Contract map: changes that must move together

### 1. Distribution, Python imports, and process entrypoints — critical

Current package metadata in `pyproject.toml` contains:

- distribution `name = "ventis"`;
- console script `ventis = "ventis.cli:main"`;
- package discovery `include = ["ventis*"]`;
- package-data ownership under `ventis`;
- Ty include/exclude paths rooted at `ventis`;
- Ty's allowed flat import `ventis_context`.

After choosing the topology, update all of those together and regenerate
`uv.lock`; do not hand-edit the lock. `uv.lock:57` already contains the CLI
`canyonos` package and `uv.lock:1225` contains the root `ventis` package, which is
direct evidence of the collision.

Runtime imports span `server.py`, `cli.py`, `stub_generator.py`, all controller
modules/providers/utilities, `OTLP_Exporter`, and `llm_proxy`. The easy-to-miss
process boundaries are:

- `ventis/Dockerfile:5-17`: `/ventis` build root, protoc paths, and
  `python -m ventis.server`;
- `ventis/server.py:9-10,56`: imports runtime helpers and spawns
  `python -m ventis.cli deploy`;
- `ventis/controller/local_controller.py:143-147`: passes runtime env and spawns
  `python -m ventis.llm_proxy`;
- `ventis/controller/instance_manager.py:14,232`: imports both provider runtimes;
- `.claude/skills/porting-to-canyonos-core/validate.py:122-141`: imports the runtime
  and probes its env-file module paths.

The bare fallbacks (`import ventis_context`, `import deploy`, generated gRPC
modules, etc.) exist because source files are copied flat into generated images.
Do not mechanically convert those to package-qualified imports without testing
both installed-source and generated-flat layouts.

### 2. Generated agent/workflow build contexts — critical

`ventis/stub_generator.py` is effectively a template engine even though it does
not use template files:

- lines 310-324 copy `llm_proxy` to `<context>/ventis/llm_proxy` and copy the
  package `__init__.py`;
- lines 395-414 and 521 copy `controller/ventis_context.py` as the flat file
  `ventis_context.py`;
- lines 443-461 emit `ENV VENTIS_AGENT_NAME` and `VENTIS_AGENT_FILE`;
- copied `local_controller.py` launches `python -m ventis.llm_proxy`;
- the collision list in `validate.py:41-46` explicitly reserves
  `ventis_context.py`.

Source, generated destination, fallback import names, validator collision rules,
and generated Dockerfile env names must change in one slice. During a compatibility
release, generated contexts may carry a small legacy import shim and both env-name
read paths. Tests must inspect the generated files and boot them; a successful
host-side import is insufficient.

### 3. Host CLI ↔ Global Controller contracts — critical

The host CLI communicates with the image over stable HTTP endpoints (`/deploy`,
`/clean`, `/status`, `/endpoints`); those endpoint paths contain no old brand and
should not be renamed.

Brand-bearing coupling that does require coordination:

- `cli/canyonos/init.py:156` injects `VENTIS_REDIS_HOST`; `ventis/server.py:85-95`
  and the controller read it. During a mixed-image transition the CLI should pass
  both names, and the new runtime should dual-read with `CANYONOS_*` precedence.
- `cli/canyonos/verify.py:43,260-262` looks for `ventis-local-*` containers and
  `ventis-*` images. It must recognize both during upgrade and switch its emitted
  guidance to CanyonOS.
- Validator JSON is a wire-like contract: `validate.py:120-126` emits
  `capabilities["ventis"]`; `cli/canyonos/verify.py:88-109` consumes it; tests in
  `tests/test_canyonos_test.py:44-50,150-173` encode it. Prefer a new neutral or
  runtime-specific key while accepting the old key for one compatibility release.
- CLI docstrings/help in `cli/cli.py`, `cli/canyonos/deploy.py`, `gc.py`,
  `verify.py`, `dashboard.compose.yml`, and `cli/ARCHITECTURE.md` still describe
  the old runtime and must follow the functional cutover.

The deploy progress parser matches message substrings rather than logger prefixes,
so renaming `logging.getLogger("ventis")` should not break its current parser.
However, `tests/test_deploy_progress.py` hardcodes many complete
`INFO:ventis...` lines and must be updated.

### 4. Environment variables — critical external API

Distinct current variables:

```text
VENTIS_AGENT_FILE
VENTIS_AGENT_HOST
VENTIS_AGENT_NAME
VENTIS_AGENT_PORT
VENTIS_DATABASE_URL
VENTIS_DEMO_SERVER_COST_MULTIPLIER
VENTIS_DEMO_TOKEN_COST_MULTIPLIER
VENTIS_DOCKER_PLATFORM
VENTIS_LC_HOST
VENTIS_LC_PORT
VENTIS_MAX_AGENT_INSTANCES
VENTIS_OTEL_DESTINATIONS
VENTIS_POLL_INTERVAL
VENTIS_PROJECT_ID
VENTIS_REDIS_HOST
VENTIS_REDIS_PORT
```

Producers include both provider runtimes, `stub_generator.py`, and the standalone
CLI's `init.py`. Consumers include deploy/future/global/local controllers,
controller frontend, server, session/telemetry logging, LLM proxy config, and root
CLI. Tests heavily patch only the legacy names today.

For a no-break transition:

1. Add a single helper for `CANYONOS_*` first / `VENTIS_*` fallback and warn once.
2. Update producers to emit new names; where old images may consume them, emit
   both temporarily.
3. Add precedence, fallback, and warning tests for every externally configurable
   variable class—not just a blind test-string rename.
4. Update docs/skill only after the new readers are released.
5. Remove the legacy branch only at the announced compatibility boundary.

`VENTIS_OTEL_DESTINATIONS` appears in docs/tests but current runtime configuration
has moved to the Redis key `otel:destinations`; confirm whether the env name is
already obsolete before adding a new alias.

### 5. Future-ID HTTP header — telemetry correctness

`ventis/llm_proxy/proxy.py:30-46` injects `X-Ventis-Future-ID`; the proxy reads it
at `ventis/llm_proxy/hooks.py:94` using different casing. HTTP header names are
case-insensitive, so the casing difference itself is safe.

**Locked:** switch injector **and** reader to the exact spelling
`X-Canyonos-Future-ID` in the same commit (no legacy header accepted). Fix the
existing casing inconsistency at the same time so both sides use `X-Canyonos-Future-ID`.
Add the currently missing producer→consumer regression test; otherwise attribution
can silently disappear while requests still succeed.

Do not rename the existing `gen_ai.*`, `project_id`, or `canyon.project.id` OTEL
attributes merely for branding. Those are separate telemetry schemas and no
`ventis`-prefixed OTEL attribute exists.

### 6. Docker, EC2, Redis, and filesystem resource names — upgrade risk

Name generation is distributed, not confined to `global_controller.py`:

- Local provider (`Local/_runtime.py:19,42-56`): network `ventis-local`, Redis
  host/container, runtime IDs, image names;
- EC2 provider (`EC2/_runtime.py:86-108,211,241-242`): AWS `Name` tags,
  `ventis-ec2-*` runtime IDs, Redis containers, images, and containers;
- Global controller (`global_controller.py:153-179,396`): stale-resource cleanup
  and Redis containers;
- root build (`ventis/cli.py:400`): image tags;
- CLI verification (`cli/canyonos/verify.py:43,260-262`): expected image/container
  names;
- Redis probe (`controller/utils/redis_utils.py:10`): exact key
  `__ventis_redis_healthcheck__`;
- remote secret copy (`controller/utils/env_file.py:61`):
  `/tmp/ventis-env-<container>`;
- Flask/logger identifiers (`server.py:12`, `controller/deploy.py:103`,
  `ventis/cli.py:22`) and user-facing controller description
  (`global_controller.py:922`).

There is also a pre-existing cleanup mismatch: global cleanup expects
`ventis-<agent>-<index>` while the Local provider launches
`ventis-local-<agent>-<index>`. Fix or explicitly account for that before using
cleanup behavior as proof of a successful rename.

A compatible upgrade must:

- stop the active deployment before switching image versions;
- discover and remove both old and new container prefixes during the transition;
- account for both network names and avoid orphaning the old network;
- make verification recognize old resources but label them as legacy;
- rebuild every agent/workflow image so generated code and the GC agree;
- update EC2 tag expectations and any operational filters;
- clean both old and new remote env-file patterns best-effort;
- test rollback using a pinned previous GC image, not mutable `latest` alone.

Runtime routing Redis keys such as `routing_table:*`, `agent:*`, `future:*`, and
`request:*` are brand-neutral and should remain unchanged. Runtime IDs stored in
those records do contain old Docker names, so an in-place Redis deployment must
not straddle versions; prefer a controlled teardown and fresh deploy.

### 7. Files, defaults, and persistent data

- SSH defaults exist in `EC2/_runtime.py:33` and
  `global_controller.py:783` as `~/.ssh/ventis_ec2`. **Locked: leave unchanged for
  now** — both stay `~/.ssh/ventis_ec2`. (These two lines are an intentional
  exception to the zero-`ventis` end state until a later pass.)
- `examples/helloworld/config/global_controller.yaml:42` uses
  `sqlite:///ventis_runtime.db`. Updating the sample does not migrate user-owned
  databases. Existing config paths should remain valid; document an optional
  user-controlled file move.
- `ventis/OTLP_Exporter/otel_queue.db` is tracked beneath the package directory.
  Preserve it across the directory move and verify whether packaging/runtime
  writes beside installed code are intentional before changing its location.
- `images/ventis-logo.png` and its `README.md` reference (plus the README clone URL)
  are **locked as unchanged for now** — deferred to a later branding pass.
- `.gitignore:30,51-52` includes old comments and `.ventis-tests/`.
- `controller/utils/env_file.py` remote temp names can leave old files after a
  crash; cleanup should understand both patterns, without broad `/tmp` deletion.

### 8. Porting skill and remote delivery

The entire vendored `.claude/skills/porting-to-canyonos-core/` tree teaches the
legacy compatibility contract. Functional changes are required in `validate.py`,
not just prose:

- import/module probes at lines 120-141;
- flat-name collision list at line 45;
- dependency-name exception at line 1002;
- capability JSON/report handling at lines 1171-1172;
- messages and command examples throughout.

The standalone CLI does not necessarily use this working-tree copy.
`cli/canyonos/build.py` downloads a skill from `SKILL_REF` and `SKILL_PATH` in the
GitHub repository. Update/publish that referenced branch/path first (or repoint it
to the merged source), then test a fresh cache. Existing local/global skill caches
can otherwise continue generating legacy scaffolding after this repo appears clean.

### 9. Tests, examples, docs, and assets

Functional test updates cover 24 test source/script files plus `tests/README.md`:

- package imports/patch targets/loggers: `test_cli.py`, `test_deploy.py`,
  `test_error_propagation.py`, `test_future.py`, controller tests, exporter tests,
  Redis/runtime tests, session/telemetry tests, and `test_ventis_context.py`;
- Docker/resource contracts: `test_canyonos_test.py`,
  `test_instance_manager_runtime.py`, `test_global_controller_redis_reuse.py`,
  and `test_runtime_ec2.py`;
- log fixtures: `test_deploy_progress.py`;
- path injection: `test_future.py`, `test_error_propagation.py`,
  `test_local_controller_metrics.py`, and `test_otel_exporter_fanout.py`;
- integration command semantics and temp/project paths: `tests/run_tests.sh`.

All four example projects contain old prose, commands, source comments, or config
defaults. Documentation cleanup includes root `README.md`, `ventis/README.md`,
`FUTURE_SCHEMA.md` by directory move, exporter/proxy/EC2 docs,
`examples/helloworld/README.md`, `tests/README.md`, CLI docs, and the complete
porting-skill tree.

Do docs/comments last. Several apparent prose strings are actually executable
examples or validator guidance and should be covered by command/import checks.

## Pre-existing blockers to establish before rename work

Record or fix these on a baseline commit so the migration has trustworthy gates:

1. `tests/run_tests.sh` invokes pytest before generating protobuf modules. Three
   tests also insert the absent `ventis/templates/grpc_stubs` path. Generate stubs
   into a deterministic test location or isolate imports with fixtures.
2. After temporary proto generation, the current suite reports 212 passed and 11
   failed. Capture the exact expected baseline or fix those failures separately.
3. Root `ventis new-project` expects a `ventis/templates` directory that no longer
   exists, while the standalone CLI uses the different `new-app` workflow.
4. Root `pyproject.toml` still has stale `templates/**/*` package-data and Ty
   exclude entries. Confirm removal versus restoration instead of carrying them
   through mechanically.
5. CI runs Ruff and Ty but not pytest, wheel-install tests, generated-context
   tests, or image builds. Passing CI currently does not prove rename safety.
6. Ignored `.venv/`, `ventis.egg-info/`, `.pytest_cache/`, generated `.car/`, and
   Docker state can preserve old entrypoints/imports. Verification must start from
   clean generated state.

## Ordered implementation plan

### Phase 0 — freeze and baseline

- Choose the package topology and compatibility window.
- Pin the current GC image by digest/tag for rollback.
- Fix or record baseline tests and deterministic proto generation.
- Add contract tests for env fallback/precedence, header fallback, validator JSON,
  generated contexts, and old/new resource discovery.

**Gate:** reproducible baseline in a clean environment, with known failures
explicitly separated from rename work.

### Phase 1 — introduce the new runtime identity  ✅ DONE (2026-09-05)

**Executed (hard cutover, package identity only):**
- `git mv ventis/ → canyonos_core/`; `controller/ventis_context.py → canyonos_context.py`;
  `tests/test_ventis_context.py → test_canyonos_context.py`.
- All `from ventis…/import ventis…` and module-path strings (test mocks, `-m` spawns,
  logger names) → `canyonos_core`; `ventis_context` alias → `canyonos_context`.
- Generated flat-copy identity in `stub_generator.py` (`ventis/llm_proxy` →
  `canyonos_core/llm_proxy`, flat `canyonos_context.py`) + fallback imports in
  `local_controller.py`/`proxy.py` so agent containers import `canyonos_core`.
- Package logger `getLogger("ventis")` → `"canyonos_core"` (+ `test_deploy_progress`,
  `test_cli` expectations); argparse `prog` → `canyonos_core`.
- `Dockerfile`: `COPY . /src`, `pip install /src`, protoc `-I/src/canyonos_core/...`,
  `ENTRYPOINT python -m canyonos_core.server`. Published image name kept `saakeths/canyonos`.
- Root `pyproject.toml` rewritten import-only: `name = canyonos-core`, no
  `[project.scripts]`, `find.include = [canyonos_core*]`, package-data key + Ty paths
  updated, stale `templates/**` dropped. **Kept** the `canyonos` (cli) editable dev-dep
  + `[tool.uv.sources]` — no longer collides now that root is `canyonos-core`, and the
  root suite imports the CLI. `uv.lock` regenerated (`ventis` gone, `canyonos-core` in).
- **Verification:** `py_compile` all tracked `.py` OK; `canyonos_core` + entrypoints
  import OK; **`uv run pytest -q` = 223 passed, 3 subtests passed, 0 failed.**

### Environment-variable phase  ✅ DONE (2026-09-05)

**Executed (hard cutover, `VENTIS_*` → `CANYONOS_*`, producers + consumers together):**
- All core env reads/writes renamed across `canyonos_core/**` (controllers, both
  provider `_runtime.py`, `future.py`, `server.py`, `deploy.py`, `llm_proxy/config.py`,
  session/telemetry logging incl. `CANYONOS_DEMO_*` multipliers) and the generated
  agent Dockerfile `ENV` in `stub_generator.py` (`CANYONOS_AGENT_NAME/FILE`).
- **Cross-boundary producer:** `cli/canyonos/init.py` now injects `CANYONOS_REDIS_HOST`
  into the GC container, matching the core reader.
- Test expectations updated (`test_deploy`, `test_instance_manager_runtime`,
  `test_global_controller_identity`, `test_session/telemetry_logging`, etc.).
- `VENTIS_OTEL_DESTINATIONS` confirmed **dead in code** (replaced by Redis key
  `otel:destinations`) — no code rename needed; only stale in docs.
- **Collision watch confirmed benign:** `CANYONOS_REDIS_HOST/PORT` is also written by
  `cli/canyonos/dashboard_stack.py`, but that targets the dashboard compose while
  `init.py` targets the GC container — different processes, no single reader of both.
- **Verification:** `uv run pytest -q` = **223 passed, 3 subtests passed, 0 failed.**
- **Still `VENTIS_*` on purpose:** only the porting-skill docs (`SKILL.md`,
  `runtime-contract.md`, `validate.py` message strings) and `OTLP_Exporter/DESIGN.md`
  — deferred to the skill/docs phase.

### Resource-name + header + validator-key + cosmetic phases  ✅ DONE (2026-09-05)

**Executed (hard cutover; every producer + consumer moved together):**
- **Resource prefixes `ventis-*` → `canyonos-*`:** core generators (both provider
  `_runtime.py`, `global_controller.py` network/redis/container names, `cli.py` image
  tags, `deploy.py`/`server.py` Flask app names, `env_file.py` `/tmp/canyonos-env-`,
  `redis_utils.py` `__canyonos_redis_healthcheck__`) **and** the user-CLI consumer
  `cli/canyonos/verify.py` (`RUNTIME_PREFIX`, image name) + all resource-name tests.
- **Future-ID header:** injector (`proxy.py`) and reader (`hooks.py`) both standardized
  to exactly `X-Canyonos-Future-ID` (fixed the old `-ID`/`-Id` casing split), plus
  `_inject_canyonos_headers`.
- **Validator capability key + framework-import check:** `caps["canyonos_core"]` /
  `capabilities.canyonos_core` / `name == "canyonos_core"` aligned across
  `validate.py` (emitter), `cli/canyonos/verify.py` (consumer), and
  `test_canyonos_test.py`.
- **Docs/prose/cosmetic:** brand sweep `Ventis`→`CanyonOS`, `ventis`→`canyonos` across
  READMEs, porting-skill docs (incl. remaining `VENTIS_*`→`CANYONOS_*`), `DESIGN.md`,
  example configs/comments, `run_tests.sh` (`canyonos_test`), `.gitignore`
  (`.canyonos-tests/`), and code comments/log strings; stale `ventis_context.py` doc
  ref → `canyonos_context.py`; `VentisContextTests` → `CanyonosContextTests`.
- **Verification:** `py_compile` all tracked `.py` OK; header injector/reader agree;
  `verify.py` prefix agrees with core generators; capability key aligned; `uv.lock`
  has zero `ventis`; **`uv run pytest -q` = 223 passed, 3 subtests passed, 0 failed.**

**Intentionally still `ventis` (operator decision):** only the EC2 SSH key default
`~/.ssh/ventis_ec2` (2 code lines + EC2 README + example config). Logo/URL were
changed by the operator directly. **No other `ventis` token remains anywhere in the
tracked tree.**

#### Porting-skill semantic caveat
The token sweep updated the skill's identifiers, but `SKILL.md` /
`runtime-contract.md` still *describe* the old names as a "compatibility protocol that
remains" — which is no longer true under the hard cutover. A follow-up semantic pass
should rewrite that framing (out of scope for a pure rename).

---

#### Original Phase 1 intent (for reference)

- Create the chosen distinct runtime distribution/import package.
- Update packaging, Ty paths, internal imports, process module paths, and root
  Dockerfile; regenerate `uv.lock`.
- If backward compatibility is promised, ship a minimal old import/command shim
  that delegates to the new runtime and warns.
- Do not let root and `cli/` both own `canyonos`.

**Gate:** isolated wheel installs prove the CLI and runtime packages can coexist;
the `canyonos` executable resolves to the standalone CLI; both new and promised
legacy imports behave as specified.

### Phase 2 — migrate generated runtime artifacts

- Update generator source/destination paths, flat context module, copied proxy
  package, local-controller process invocation, Dockerfile env, and validator
  collision rules as one unit.
- Rebuild all generated contexts from scratch; never reuse old output.

**Gate:** generated agent and workflow contexts contain the intended package/env
names, import their controller/proxy, load an example agent, and boot in Docker.

### Phase 3 — migrate protocol identifiers compatibly

- Add new-first/old-fallback env reads and header reads.
- Change producers, including CLI `init.py` and generated Dockerfiles.
- Version the validator capability JSON transition and update CLI verification.
- Publish the updated remote porting skill and test an empty cache.

**Gate:** old CLI/new image and new CLI/old image combinations either work within
the declared matrix or fail early with a precise version error; telemetry
attribution remains intact.

### Phase 4 — migrate operational resource names

- Change Local/EC2 image, container, network, runtime ID, Redis container, AWS tag,
  healthcheck, and remote env-file names.
- Update GC cleanup and CLI verification together, recognizing both generations
  for the compatibility release.
- Resolve the existing Local stale-cleanup mismatch.

**Gate:** fresh local deploy, EC2 mocked/probe tests, upgrade teardown, verify,
clean, and rollback all leave no unexpected containers/networks/temp files.

### Phase 5 — publish and switch

- Build and inspect both wheels/sdists in clean environments.
- Build the GC image from the renamed runtime, pin a versioned tag/digest, smoke
  `/status`, then update `GC_IMAGE`/release metadata.
- Run a representative end-to-end workflow through build, deploy, request, status,
  telemetry, verify, stop, and quit.

**Gate:** the published artifacts—not editable source installs—pass the full
matrix on a clean machine or clean VM.

### Phase 6 — cosmetic cleanup and later compatibility removal

- Update prose, help, examples, comments, ASCII/logo assets, and test names.
- After the announced compatibility period, remove shims/fallbacks and legacy
  resource discovery in a major release.
- Rename/archive this migration document, recreate all generated state, and run
  the final forbidden-token scan.

## Acceptance matrix

| Layer | Required proof |
|---|---|
| Static tree | No old token/path outside an explicit temporary compatibility allowlist |
| Lock/metadata | `uv lock` succeeds; wheel metadata has distinct owners/names |
| Clean installs | CLI and runtime wheels coexist; import paths and script owner are exact |
| Type/lint | Ruff and Ty pass with renamed include/exclude/unresolved-import paths |
| Unit tests | Protos generated deterministically; rename does not add failures |
| Generated output | Context contains new proxy/context/env names and no accidental stale package |
| Header telemetry | New header attributes correctly; legacy fallback works during transition |
| Env contract | New-wins precedence and every promised legacy fallback are tested |
| Local runtime | Image/network/Redis/container names agree with `canyonos verify` |
| EC2 runtime | Tags, image/container names, SSH fallback, and remote temp cleanup agree |
| Fresh deploy | Build → deploy → request → poll → telemetry → verify → teardown succeeds |
| Upgrade deploy | Old resources are detected/removed; no mixed-version silent failure |
| Rollback | Previous pinned image can be restored without deleting user DBs/keys/config |
| Remote skill | Fresh download/cache teaches and validates the new contract |
| Published image | New Docker entrypoint imports and `/status` answers from the released tag |

Suggested final scans (the migration file and explicitly approved compatibility
shim are the only temporary exceptions):

```bash
git grep -I -i -n ventis -- . ':!VENTIS_TO_CANYONOS_RENAME.md'
git ls-files | rg -i 'ventis'
rg --hidden --no-ignore -i ventis \
  -g '!.git/**' -g '!VENTIS_TO_CANYONOS_RENAME.md'
find . -path './.git' -prune -o -iname '*ventis*' -print
```

## Rollback rules

- Never make `latest` the only rollback reference; retain the previous image
  digest and compatibility matrix.
- Stop the deploy before changing resource prefixes. Do not run old and new GCs
  against one Redis state concurrently.
- Preserve user config, `.env`, SSH keys, SQLite databases, and OTEL data. Rename
  or copy user-owned files only on explicit user action.
- Keep cleanup exact and prefix-scoped; never broadly delete Docker or `/tmp`
  state.
- If the new image fails, tear down only resources created by that attempt,
  restore the previous pinned image, and use legacy env/header/resource support
  until the failure is fixed.

## Complete matching-file coverage

The scan includes all old-name matches in these groups:

- **Runtime (36):** the package Dockerfile; package README; exporter design/source;
  package/controller initializers; root runtime CLI/server/stub generator; Local and
  EC2 runtime/readme; deploy/future/global/instance/local controllers; env,
  process-supervisor, Redis, session, and telemetry utilities; the LLM proxy README,
  entrypoint, app/config/core/hooks/proxy, and all provider modules.
- **Tests (25):** `tests/README.md`, `run_tests.sh`, `test_canyonos_test.py`,
  `test_cli.py`, `test_deploy.py`, `test_deploy_progress.py`,
  `test_error_propagation.py`, `test_future.py`, every `test_global_controller_*`,
  `test_gpu_metrics.py`, `test_instance_manager_runtime.py`, both
  `test_local_controller_*`, both exporter tests, Redis/EC2/session/stub/telemetry
  tests, and `test_ventis_context.py`.
- **Examples (15):** `examples/helloworld/README.md`; finance agent/config/workflow;
  helloworld config/workflow; portfolio advisor/intent/metrics agents plus
  config/workflow; text2sql generator/vLLM agents plus config/workflow.
- **Porting skill (7):** `SKILL.md`, `validate.py`, and the EC2, LLM proxy,
  packaging, runtime-contract, and troubleshooting references.
- **Standalone CLI (7):** `cli/ARCHITECTURE.md`, `cli/cli.py`, and CanyonOS
  dashboard compose, deploy, GC, init, and verify modules.
- **Root (4):** `.gitignore`, `README.md`, `pyproject.toml`, and `uv.lock`.

No additional `setup.py`, `setup.cfg`, package manifest, Dockerfile, or compose
file contains the old token. `requirements.txt` has no project-name match. Binary
inspection found no embedded old token in the tracked SQLite/JPEG/PNG assets; the
PNG still requires a filename/reference rename because its basename is branded.
