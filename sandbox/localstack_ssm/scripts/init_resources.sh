#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ROOT
cd "$ROOT"

export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-test}
export AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-test}
export AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN:-test}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-us-east-1}
LOCALSTACK_ENDPOINT=${VENTIS_LOCALSTACK_ENDPOINT:-http://localhost:4566}
AMI_ID=${VENTIS_LOCALSTACK_AMI_ID:-ami-0c0ffee000000001}

"$ROOT/scripts/start_localstack.sh"

awsls() {
  if command -v awslocal >/dev/null 2>&1; then
    awslocal "$@"
  else
    aws --endpoint-url "$LOCALSTACK_ENDPOINT" "$@"
  fi
}

json_field() {
  python -c 'import json,sys; data=json.load(sys.stdin); expr=sys.argv[1].split("."); cur=data
for part in expr:
    if not part:
        continue
    if part.isdigit():
        cur=cur[int(part)]
    else:
        cur=cur.get(part)
print("" if cur is None else cur)' "$1"
}

vpc_json=$(awsls ec2 describe-vpcs)
vpc_id=$(printf '%s' "$vpc_json" | json_field 'Vpcs.0.VpcId')
if [[ -z "$vpc_id" ]]; then
  vpc_id=$(awsls ec2 create-vpc --cidr-block 10.0.0.0/16 | json_field 'Vpc.VpcId')
fi

subnet_json=$(awsls ec2 describe-subnets --filters Name=vpc-id,Values="$vpc_id")
subnet_id=$(printf '%s' "$subnet_json" | json_field 'Subnets.0.SubnetId')
if [[ -z "$subnet_id" ]]; then
  subnet_id=$(awsls ec2 create-subnet --vpc-id "$vpc_id" --cidr-block 10.0.1.0/24 | json_field 'Subnet.SubnetId')
fi

sg_json=$(awsls ec2 describe-security-groups --filters Name=vpc-id,Values="$vpc_id")
sg_id=$(printf '%s' "$sg_json" | python -c 'import json,sys; groups=json.load(sys.stdin).get("SecurityGroups", []); match=next((g.get("GroupId") for g in groups if g.get("GroupName") == "default"), None); print(match or (groups[0].get("GroupId") if groups else ""))')
if [[ -z "$sg_id" ]]; then
  sg_id=$(awsls ec2 create-security-group --group-name ventis-localstack-ssm --description ventis-localstack-ssm --vpc-id "$vpc_id" | json_field 'GroupId')
fi

set +e
awsls ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions '[{"IpProtocol":"tcp","FromPort":50051,"ToPort":50051,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' >/dev/null 2>&1
set -e

cat > "$ROOT/sandbox.env" <<EOF
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN
AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION
VENTIS_LOCALSTACK_ENDPOINT=$LOCALSTACK_ENDPOINT
VENTIS_LOCALSTACK_AMI_ID=$AMI_ID
VENTIS_LOCALSTACK_VPC_ID=$vpc_id
VENTIS_LOCALSTACK_SUBNET_ID=$subnet_id
VENTIS_LOCALSTACK_SECURITY_GROUP_ID=$sg_id
EOF

export VENTIS_LOCALSTACK_AMI_ID=$AMI_ID
export VENTIS_LOCALSTACK_SUBNET_ID=$subnet_id
export VENTIS_LOCALSTACK_SECURITY_GROUP_ID=$sg_id
python - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["ROOT"])
template = (root / "project/config/global_controller.localstack.template.yaml").read_text()
content = (
    template
    .replace("__AMI_ID__", os.environ["VENTIS_LOCALSTACK_AMI_ID"])
    .replace("__SUBNET_ID__", os.environ["VENTIS_LOCALSTACK_SUBNET_ID"])
    .replace("__SECURITY_GROUP_ID__", os.environ["VENTIS_LOCALSTACK_SECURITY_GROUP_ID"])
)
(root / "project/config/global_controller.localstack.generated.yaml").write_text(content)
PY

echo "Wrote $ROOT/sandbox.env"
echo "Wrote $ROOT/project/config/global_controller.localstack.generated.yaml"
