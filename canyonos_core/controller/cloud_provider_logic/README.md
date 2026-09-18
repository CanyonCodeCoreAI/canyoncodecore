# Cloud Provider Logic

Backend implementations for where CanyonOS runs an agent's container. Every agent
in `global_controller.yaml` picks one via `provider: local` or `provider: EC2`.

What each provider differs in is where the launched agent lives in, local lives in the host terminal and EC2 would spawn in another EC2 instance.

## Providers

| Provider | Folder | Compute |
| --- | --- | --- |
| `local` (default) | `Local/` | Docker container on the same machine, the root README has the whole flow. |
| `EC2` | `EC2/` | One EC2 instance per replica. |

See [EC2/README.md](EC2/README.md) for EC2-specific setup.
