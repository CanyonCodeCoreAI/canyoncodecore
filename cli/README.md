Lightweight CLI for CanyonOS

Serves as a thin API layer, connecting to the global controller container.

## Serve

`canyonos serve` starts the local CanyonOS dashboard against the Postgres bundled in its compose
stack — it reads no project config, so it takes no arguments. It writes only `CANYONOS_`-prefixed
settings into the current directory's `.env`, leaving every other line unchanged.

## Requirements
Need a coding agent(Claude Code, Codex, Cursor)
Need uv or pip
Need docker and docker compose

If you have a workflow running, and want to make a config change, canyonos config automatically would reload the project with your config. If you change the workflow files itself though and want the changes to take effect, you need to redeploy from scratch, running canyonos build for good measure

# Use: canyonos -h
### To Republish to PyPi

```Terminal
cd cli
# Go into, pyproject.toml, and increment version number 
rm -rf dist/ # Removes the old distro, causes conflicts

uv build
uv publish # Needs PyPi Auth Token, ask Saaketh
```