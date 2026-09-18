# Developing the CLI

The commands are the same in development and production. The only differences
are where the binary runs from and an optional `packages/cli/.env`:

| | Dev | Prod |
|---|---|---|
| How it runs | `uv run canyonos deploy` from the workspace | `canyonos deploy` |
| Environment | `development` via `packages/cli/.env` (copied once from `packages/cli/.env.example`, gitignored) | `production` by default, no `.env` |
| Core image | `canyonos-core:dev` local | `ghcr.io/…/canyonos-core:latest` |
| Skill | `.claude/skills/porting-to-canyonos` from the checkout | downloaded from `main` |

## Setup

```bash
cp packages/cli/.env.example packages/cli/.env  # once; the file is gitignored
uv sync                                         # installs every workspace member editable
uv run canyonos doctor
```

`uv run canyonos <command>` works for every command from anywhere in the
workspace, because the root `.venv` holds both packages. There are no dev-only
flags. `packages/cli/.env` is read from next to `packages/cli/cli.py`, never
from the directory you run in, and a variable already set in your shell wins
over the file.

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
| `CANYONOS_SKILL_SOURCE` | `<workspace root>/.claude/skills/porting-to-canyonos` |
| `CANYONOS_API_IMAGE` | `canyonos-api:dev` |
| `CANYONOS_WEB_IMAGE` | `canyonos-web:dev` |

## Building the core image

`CANYONOS_CORE_IMAGE=local` expects an image the daemon already has, so build it
after changing anything under `packages/core/canyonos_core/`. The build context
is the core package:

```bash
docker build -f packages/core/Dockerfile -t canyonos-core:dev packages/core
```

`canyonos deploy` uses a local or literal image if the daemon has it and only
pulls as a fallback; it never builds one for you. The production image is always
pulled, exactly as a released CLI does. The dashboard images are built in the
separate `canyon-os` repo.

## Tests

Every suite runs from the workspace root through Turborepo:

```bash
bun run test
```

`bun run check` runs lint, type checks and the format check the same way. See
`tests/README.md` for the end-to-end harness, which needs a live stack.
