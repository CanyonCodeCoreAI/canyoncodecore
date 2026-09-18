# EC2 Specific Set-up

The backend for `provider: EC2` agents. It launches one EC2 instance per
replica, ships the agent's built Docker image to it over SSH, and starts the
container there. 

## What the host needs to deploy to EC2

** The host means the machine that you are running canyonos deploy on **

- Everything needed for local deployment. (This is Docker and the canyonos CLI)
- AWS Credentials. The host needs IAM permissions to execute certain EC2 commands. The `AmazonEC2FullAccess` policy in IAM covers all of the needed permissions. For the exact IAM permissions needed, look below in the `IAM Permissions` section.
- Config block added to `global_controller.yaml` (shown below). The security group would also need to allow specific ports in.

## AWS-side setup

- **Security group** (everything is inbound, same permissions needed for outbound unless you allow all outbound traffic)
  - The workflow and dashboard port need to be opened up, they are by default 8080 and 8081, and are referred as such below
  - Port 50051, 6379, and 22 needs to be accessible by the security group, as well as wherever the host runs.
  - Port 8080 needs be accessible by whoever queries the workflow (0.0.0.0 for example)

## Config (`ec2:` block in `global_controller.yaml`)

You need to add this block to global_controller.yaml, to give canyonos the necessary credentials to launch agents on EC2.

```yaml
ec2:
  region: us-east-1
  subnet_id: subnet-0123456789abcdef0
  security_group_ids:
    - sg-0123456789abcdef0
```

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
  - PassRole (only needed if you set `ec2.instance_profile_name`)

You can paste the exact JSON below in the "create manual policy" field in IAM when generating permissions for a role. Drop the `iam:PassRole` statement if you're not setting `ec2.instance_profile_name`.

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
    }
  ]
}
```


## [OPTIONAL] Extra Configurations in the ec2 block in `global_controller.yaml`

The ec2 config block given above has the bare minimum needed to get the instances up and running, to add separate settings, you can add these following fields in the block.

```yaml
ec2:
  # Required Commands
  region: us-east-1
  subnet_id: subnet-0123456789abcdef0
  security_group_ids:
    - sg-0123456789abcdef0

  # Optional Commands
  instance_profile_name: ec2launch # For the launched instances, if you want a specific IAM role attached to each instance, pass this variable in.
  ami_id: ami-0123456789abcdef0 # Look at `Creating your own AMI` below
  ssh_user: ubuntu # Look at `Creating your own AMI` below
  ssh_private_key_path: ~/.ssh/your-own-key # Look at `Private Key` below
```
For the instance_profile_name, you will also need to add a new policy to your IAM role in the host machine, the iam:PassRole policy. JSON below.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<account-id>:role/<role-behind-your-instance-profile>"
    }
  ]
}
```

## [OPTIONAL] Private Key

`ec2.ssh_private_key_path` is optional, defaulting to `~/.ssh/canyonos_ec2`.
If nothing exists at that default path, canyonos generates a fresh ed25519
keypair there for you on first use (readable only by its owner: `chmod 600`).
If you set `ec2.ssh_private_key_path` to your own path instead, that file
must already exist — canyonos only auto-generates the default, never a
path you explicitly configured.

The AWS-side key pair is always automatic either way: canyonos imports
whichever key you end up with (generated or your own) into AWS on first use,
named `canyonos-ec2-<project_id>-<pubkey hash>`, so the AMI never needs the
key pre-authorized.

## [OPTIONAL] Creating your own AMI

We have provided our own base AMI_ID: ami-0101d5f2a2a9cd55c, but if you want to create your own, the ami you create just needs to have docker and zstd installed. The commands to install it are below.
If using a different AMI base than Ubuntu, you will need to change the ssh_user manually.

```bash

#!/bin/bash
set -eux

apt-get update
apt-get install -y docker.io zstd

systemctl enable docker
systemctl start docker

```