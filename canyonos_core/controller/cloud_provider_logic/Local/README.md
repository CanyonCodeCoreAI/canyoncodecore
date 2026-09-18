# Local Set-up

The default backend for `provider: local` agents. Runs each agent as a Docker
container on the same machine where you run `canyonos deploy`.

## What you need

- Docker running locally. That's it — the `canyonos-local` network and each
  Redis container are created for you automatically.

## Config

| Key | Where | Default | Notes |
| --- | --- | --- | --- |
| `host_port` / `port` | per agent | next free port from `8000` | Port on the host that maps to the container. |
