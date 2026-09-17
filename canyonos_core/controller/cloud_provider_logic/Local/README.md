# Local Set-up

The default backend for `provider: local` agents. Runs each agent as a Docker
container, normally on the same machine you run `canyonos deploy` on.

## What you need

- Docker running locally. That's it — the `canyonos-local` network and each
  host's Redis container are created for you automatically.

## Remote hosts (optional)

You're not limited to your own machine — point an agent at another box you
already have running by setting `host` (and `user`, if needed) on that agent:

```yaml
  - name: MetricsAgent
    provider: local
    host: 10.0.0.12
    user: ubuntu
```

That machine needs:
- Docker running, reachable without a sudo password prompt over SSH.
- Passwordless SSH access from the machine you deploy from, using the key at
  `ec2.ssh_private_key_path` (default `~/.ssh/ventis_ec2`) — set this even if
  you have no EC2 agents, since remote `local` hosts use the same key.

## Config

| Key | Where | Default | Notes |
| --- | --- | --- | --- |
| `host` | per agent | `localhost` | Point this at a remote machine to run that agent there instead. |
| `host_port` / `port` | per agent | next free port from `8000` | Port on the host that maps to the container. |
| `user` | per agent | — | SSH user for a remote `host`. Leave unset for `localhost`. |
