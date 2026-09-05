# EC2 deployment

Read this only when at least one config entry uses `provider: EC2`.

## Configuration

Use `provider: EC2` and declare `instance_type` on every EC2 service entry. The
top-level `ec2` block supplies the runtime's required infrastructure and SSH
settings. Read the target checkout's deploy preflight and EC2 runtime before
writing the block; do not copy values from an example environment.

Typical required categories are:

- AMI and instance type
- region and subnet
- security groups
- SSH user and credentials accepted by the runtime

`ventis deploy` owns basic EC2 config validation. A preflight pass is not proof
that provisioning, SSH, image transfer, or remote container startup works.

## Networking

A remote container's `host.docker.internal` names its own EC2 Docker host. It
does not name the local controller machine. Databases, model proxies, and other
services must use addresses reachable from every selected host.

The environment file may be copied temporarily to a remote host by runtimes that
expose the `env_file` capability. Confirm behavior from the capability probe and
target runtime rather than assuming local Docker semantics.

## Probes and cleanup

Run the same runtime and adapter probes against the exact image before remote
deployment. After deploy, verify the remote container logs; controller health
can be green even when agent loading failed.

Stop foreground deploy normally so the controller can terminate recorded EC2
instances. If provisioning or startup fails before an instance is recorded,
inspect the cloud provider directly and remove exact leaked resources. Never use
a broad cleanup command against unrelated instances.
