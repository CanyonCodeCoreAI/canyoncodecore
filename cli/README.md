Lightweight CLI for CanyonOS

Serves as a thin API layer, connecting to the global controller container.

## Architecture

For a full walkthrough of the `build`, `deploy`, and `config` flows — plus how
`logs`, `stop`, and `quit` fit into the container lifecycle — see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Serve

`canyonos serve` starts the local CanyonOS dashboard against the Postgres bundled in its compose
stack — it reads no project config, so it takes no arguments. It writes only `CANYONOS_`-prefixed
settings into the current directory's `.env`, leaving every other line unchanged.

## Requirements
Need a coding agent(Claude Code, Codex, Cursor)
Need uv or pip
Need docker and docker compose

If you have a workflow running, and want to make a config change, canyonos config automatically would reload the project with your config. If you change the workflow files itself though and want the changes to take effect, you need to redeploy from scratch, running canyonos build for good measure
