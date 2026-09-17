# EC2 Specific Set-up

The backend for `provider: EC2` agents. It launches one EC2 instance per
replica, ships the agent's built Docker image to it over SSH, and starts the
container there. 

## What the host needs

** The host means the machine that you are running canyonos deploy on **

- Docker running locally — the agent image is built and `docker save`d here before transfer.
- `zstd` on `$PATH` — the image is piped through it before the SSH transfer.
- AWS Credentials. Would need the IAM permissions to execute certain EC2 commands on the machine. The ec2_launcher role in IAM covers all of the needed permissions. For exact knowledge, look below in the `IAM Permissions` section.

## AWS-side setup

- **IAM instance profile** named exactly `ec2launch` — hardcoded in
  `provision_instance`'s `run_instances` call, so the account must have a
  profile by this literal name. Its role should grant whatever the agent
  code itself needs on AWS (e.g. Bedrock, if not going through the LLM proxy).
- **Security group** (`ec2.security_group_ids`) allowing inbound TCP `22` (SSH),
  `50051` (agent gRPC / health check), and each agent's `redis_port` (default
  `6379`) from the deploying host. A `type: workflow` agent's `api_port`
  (default `8080`) needs inbound access too if you're calling it from off-box.

## Config (`ec2:` block in `global_controller.yaml`)

You need to add this block to global_controller.yaml, to give the deploy the necessary credentials to launch agents on EC2.

```yaml
ec2:
  region: us-east-1
  subnet_id: subnet-0123456789abcdef0
  security_group_ids:
    - sg-0123456789abcdef0
```

Values also accept `${VAR}` interpolation from `env_file` (see `examples/portfolio`).


`canyonos build` handles this automatically, but each EC2 agent's spec also needs its own `instance_type` (e.g. `t3.micro`), example below. 

Example:

```yaml
  - name: ExampleAgent
    entrypoint: agents/example_agent.py
    provider: EC2
    instance_type: t3.micro
```

## IAM permissions

The host machine needs these IAM Permissions to be able to launch external EC2 instances. This can either be held by the host itself, or the EC2 machine hosting the deployment.

- ec2:
  - RunInstances
  - TerminateInstances
  - DescribeInstances
  - CreateTags
  - ImportKeyPair
- IAM:
  - PassRole

You can paste the exact JSON below in the "create manual policy" field in IAM when generating permissions for a role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:DescribeInstances",
        "ec2:CreateTags",
        "ec2:ImportKeyPair"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<account-id>:role/<role-behind-ec2launch>"
    }
  ]
}
```

## [OPTIONAL] Private Key

By default, canyonos creates a new private key for every project you deploy (The key pair is named `canyonos-ec2-<project_id>-<pubkey hash>`), but you can set your own key pair in the global_controller.yaml file in the ec2 block.
In addition to the security group and subnet_id, if you place `ssh_private_key_path: path/to/private/key` there, the key used will instead be that.

## [OPTIONAL] Creating your own AMI

We have provided our own base AMI_ID: ami-0101d5f2a2a9cd55c, but if you want to create your own, the ami you create just needs to have docker and zstd installed. The commands to install it are below.

```bash

#!/bin/bash
set -eux

apt-get update
apt-get install -y docker.io zstd

systemctl enable docker
systemctl start docker

```