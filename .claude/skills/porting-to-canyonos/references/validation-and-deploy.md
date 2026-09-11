# Validate, hand off, and optionally deploy

**When:** after runtime code and configuration are complete.

**Output:** a clean validation report and a stopped porting workflow. Deployment
is a separate action that requires explicit approval.

## Validation scope

Validate only contracts that authored port files can violate and CanyonOS does
not fail closed on—for example declaration/adapter bindings, generated-stub
imports, workflow call shape, per-image dependency coverage, and
capability-dependent behavior.

Do not add duplicate checks for postconditions strongly guaranteed by code:

- a successful `prepare.py` run already creates the artifact directories,
  rejects source symlinks, applies exclusions, and installs the copy atomically;
- `canyonos config` owns the structure of configuration it generates;
- deploy preflight owns checks that already fail before resources are changed.

Treat required inputs such as a readable manifest and `.car/app` as validator
preconditions, not independent port rules. If a guarantee changes in CanyonOS,
change the owning code or capability probe rather than maintaining a parallel
rule in prose and validation.

## Run the gap validator

From the application root, run:

```bash
python3 <skill_dir>/validate.py .car
```

Fix every `ERROR` and rerun until the command exits 0. Do not hide warnings or
capability limitations: list each in the handoff and state whether it blocks
this source. Confirm with `git status` that no developer-owned file outside
`.car` changed.

The full-project-file-sweep check is gated on importing `canyonos_core`. Its
UNAVAILABLE result is expected from a standalone `canyonos` CLI installation.
From a Core checkout or an environment with `canyonos-core` installed, an
unexpected import failure may be an environment problem; do not dismiss it.

Report:

- that the `.car` port validated;
- files created;
- validator warnings;
- unresolved runtime blockers;
- intentionally omitted unreachable dependencies or source surfaces.

Then stop and ask exactly one direct approval question:

> Validation passed. Run `canyonos deploy` now? This will build images and start
> the deployment.

Do not treat silence, an unattended run, or the original request to “port” as
approval.

## Deploy only after approval

If the user explicitly approves, run from the application root:

```bash
canyonos deploy
```

Do not run a standalone build first: `canyonos deploy` performs both build and
deployment. Do not add probing, deployment debugging, or cleanup to the porting
flow.

If an approved deploy fails during build, startup, or a request, read
`troubleshooting.md`. Read `runtime-contract.md` when a validator finding or
runtime mechanism needs explanation.
