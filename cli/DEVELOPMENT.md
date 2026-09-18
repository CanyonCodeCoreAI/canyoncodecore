# Developing the CLI

The commands are the same in development and production. The only differences
are where the binary runs from and an optional `cli/.env`:

| | Dev | Prod |
|---|---|---|
| How it runs | `cd cli && uv run canyonos deploy` | `canyonos deploy` |
| Environment | `development` via `cli/.env` (copied once from `cli/.env.example`, gitignored) | `production` by default, no `.env` |
| Core image | `canyonos-core:dev` local | `ghcr.io/…/canyonos-core:latest` |
| Skill | `.claude/skills/porting-to-canyonos` from the checkout | downloaded from `main` |

## Setup

```bash
cp cli/.env.example cli/.env     # once; the file is gitignored
cd cli && uv run canyonos doctor # uv installs the project editable
```

`uv run canyonos <command>` works for every command; there are no dev-only
flags. `cli/.env` is read from next to `cli/cli.py`, never from the directory
you run in, and a variable already set in your shell wins over the file.

## The environment

`CANYONOS_ENV` is `development`, `test` or `production`; unset or empty means
`production`. Anything else fails at startup. `canyonos/env.py` is the only
module that reads the environment: it resolves each artifact once and everything
else imports the result.

Every artifact variable takes the same three forms:

| Value | Meaning |
|---|---|
| unset or `prod` | the production artifact, i.e. exactly what a released CLI uses |
| `local` | the artifact from this checkout |
| anything else | taken literally: an image tag, a directory, or a git ref for the skill |

Artifacts are independent, so "core from my checkout, skill from `main`" and the
reverse are both fine. Outside `production` overrides are allowed; in
`production` anything but unset/`prod` raises, so a released CLI can never be
talked into a dev artifact.

| Variable | `local` resolves to |
|---|---|
| `CANYONOS_CORE_IMAGE` | `canyonos-core:dev` |
| `CANYONOS_SKILL_SOURCE` | `<repo root>/.claude/skills/porting-to-canyonos` |
| `CANYONOS_API_IMAGE` | `canyonos-api:dev` |
| `CANYONOS_WEB_IMAGE` | `canyonos-web:dev` |

## Building the core image

`CANYONOS_CORE_IMAGE=local` expects an image the daemon already has, so build it
from the repo root after changing anything under `canyonos_core/`:

```bash
docker build -f canyonos_core/Dockerfile -t canyonos-core:dev .
```

`canyonos deploy` uses a local or literal image if the daemon has it and only
pulls as a fallback; it never builds one for you. The production image is always
pulled, exactly as a released CLI does. The dashboard images are built in the
separate `canyon-os` repo.

## Tests

The CLI suites run from `cli/`:

```bash
uv run --with pytest --with pyyaml python -m pytest \
  ../tests/test_canyonos_env.py ../tests/test_cli.py ../tests/test_dashboard_stack.py
```

The suites covering the core runtime need the root project's dependencies
instead; see `tests/README.md`.
