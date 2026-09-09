CLI for CanyonOS

This CLI does not contain much logic, instead serving as an API to interface with the canyonos container that deploys and runs your entire workflow


## Requirements
Need a coding agent(Claude Code, Codex, Cursor)
Need uv or pip
Need docker and docker compose

## Install

Via pip/uv:
```
pip install canyonos
```

Via curl (downloads a standalone binary, macOS/Linux, no Python required):
```
curl -fsSL https://raw.githubusercontent.com/CanyonCodeCoreAI/canyoncodecore/main/cli/install.sh | sh
```

Via Homebrew (same standalone binary):
```
brew tap CanyonCodeCoreAI/canyonos https://github.com/CanyonCodeCoreAI/canyoncodecore
brew install canyonos
```
The formula lives at `Formula/canyonos.rb` in this repo, so no separate `homebrew-*` tap repo is
needed.

The binaries are built and attached to GitHub releases by `.github/workflows/cli-release.yml`,
triggered automatically by `.github/workflows/cli-release-tag.yml` when `cli/pyproject.toml`'s
version changes on `main` (or by pushing a `cli-v*` tag by hand). After each release, update
`Formula/canyonos.rb`'s `version` and the three `sha256` values (`shasum -a 256 <downloaded binary>`)
to match.

## Architecture

For a full walkthrough of the `build`, `deploy`, and `config` flows — plus how
`logs`, `stop`, and `quit` fit into the container lifecycle — see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Serve

`canyonos serve` starts the local CanyonOS dashboard — it reads no project config, so it takes no
arguments. It writes only `CANYONOS_`-prefixed settings into the current directory's `.env`,
leaving every other line unchanged.


If you have a workflow running, and want to make a config change, canyonos config automatically would reload the project with your config. If you change the workflow files itself though and want the changes to take effect, you need to redeploy from scratch, running canyonos build for good measure

# Use: canyonos -h
