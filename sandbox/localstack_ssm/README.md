# LocalStack EC2 + SSM smoke sandbox

Linux only. LocalStack's EC2 Docker VM manager exposes instance bridge networking to the host on Linux, but not on macOS Docker Desktop.

## Prereqs

- Linux host with Docker + Docker Compose
- Python environment that can run `python -m ventis.cli`
- LocalStack Pro auth token exported as `LOCALSTACK_AUTH_TOKEN`
- AWS CLI or `awslocal` on PATH

## Setup

```bash
cd sandbox/localstack_ssm
cp .env.example .env
$EDITOR .env  # set LOCALSTACK_AUTH_TOKEN
set -a && source .env && set +a
```

## Run

```bash
./scripts/start_localstack.sh
./scripts/build_ami.sh
./scripts/init_resources.sh
./scripts/smoke.sh
```

## Run on macOS

macOS cannot do host-to-instance TCP checks against LocalStack EC2 container IPs, so use the separate bootstrap-only smoke:

```bash
./scripts/smoke_macos.sh
```

This macOS path verifies:
- two LocalStack EC2 instances launched,
- SSM bootstrap succeeded on both,
- both Ventis agent containers are running in `container:localstack-ec2.<instance-id>` network mode,
- both local controllers reported `healthy` into a host Redis container.

It intentionally does **not** verify direct host access to `<instance-ip>:50051` or end-to-end workflow routing through the global controller.

The smoke script will:
- build the Ventis smoke project,
- deploy one host workflow plus two EC2-backed agent replicas,
- verify two LocalStack instances exist,
- verify SSM bootstrap completed,
- verify both local controllers answer on `<instance-ip>:50051`,
- verify a real workflow query succeeds through the host/global controller.

## Cleanup

```bash
./scripts/smoke.sh cleanup
docker compose down -v
```

## Generated files

- `sandbox.env` — discovered AWS/LocalStack values
- `project/config/global_controller.localstack.generated.yaml` — concrete Ventis config used by the smoke run
- `.deploy.pid` / `.deploy.log` — background deploy process state

## Notes

- The custom AMI is tagged using LocalStack's documented Docker AMI scheme: `localstack-ec2/<name>:<ami-id>`.
- The SSM bootstrap path intentionally stays minimal: it assumes the Ventis agent image already exists on the shared Docker daemon.
- Default endpoint is fixed to `http://localhost:4566`.
- Linux remains the full end-to-end smoke path. macOS is bootstrap-only because Docker Desktop does not expose the LocalStack EC2 bridge network to the host.

## LocalStack docs

- EC2 Docker VM manager: https://docs.localstack.cloud/aws/services/ec2/
- SSM with Docker-backed EC2: https://docs.localstack.cloud/aws/services/ssm/
- Docker Compose + Pro auth token: https://docs.localstack.cloud/aws/getting-started/installation/
- Endpoint/networking notes: https://docs.localstack.cloud/aws/configuration/networking/accessing-endpoint-url/
