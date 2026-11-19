#!/bin/bash
# Ship the current code to the AegisOps EC2 instance (keeps .env secrets and data volumes).
#   bash infra/aws/redeploy.sh
set -euo pipefail
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # AWS CLI on Windows: print build logs without charmap errors
cd "$(dirname "$0")/../.."
PY=$( [ -x .venv/Scripts/python ] && echo "$PWD/.venv/Scripts/python" || echo python3 )
$PY scripts/package_share.py
cd infra/aws/terraform
eval "$(aws configure export-credentials --format env)"   # temp creds from `aws login`, never printed
MSYS_NO_PATHCONV=1 terraform apply -input=false -auto-approve -no-color | grep -E "Plan:|Apply complete|Error"
REGION=ap-south-1
INSTANCE=$(terraform output -raw instance_id)
BUCKET=$(terraform state show -no-color aws_s3_bucket.bundle | awk -F'"' '/^ +bucket +=/{print $2}')

read -r -d '' REMOTE <<EOS || true
set -e
cd /opt/aegisops
aws s3 cp s3://$BUCKET/aegisops-share.zip bundle.zip --region $REGION
rm -rf app && unzip -q bundle.zip && mv aegisops app && rm bundle.zip
cd app && bash infra/aws/render_env.sh $REGION /aegisops
docker compose -f docker-compose.yml -f docker-compose.aws.yml up -d --build --remove-orphans --force-recreate
# ShopFront: separate Compose project on the shared aegis-net network (its data volume is kept)
cd shopfront && SHOPFRONT_BUILD_TIME=\$(date +%s) docker compose --env-file ../.env \
  -f docker-compose.yml -f docker-compose.aegis.yml up -d --build --remove-orphans && cd ..
docker image prune -f >/dev/null
docker compose -f docker-compose.yml -f docker-compose.aws.yml ps
docker compose -p shopfront ps
EOS

ID=$(MSYS_NO_PATHCONV=1 aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --timeout-seconds 3600 \
  --parameters "$($PY -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":["3600"]}))' "$REMOTE")" \
  --query Command.CommandId --output text)
echo "Redeploying on $INSTANCE (command $ID)..."
DEADLINE=$((SECONDS + 1800))   # a full rebuild takes ~5-10 min
while :; do
  # bounded per call; errors are printed, not hidden (a silent retry loop once ran 27 min past the remote finish)
  S=$(aws ssm get-command-invocation --region "$REGION" --command-id "$ID" --instance-id "$INSTANCE" \
      --cli-connect-timeout 10 --cli-read-timeout 20 --query Status --output text) || S=Pending
  case "$S" in Success|Failed|Cancelled|TimedOut) break;; esac
  [ "$SECONDS" -ge "$DEADLINE" ] && { echo "Stopped waiting after 30 min (last status: $S). Command $ID may still be running."; break; }
  sleep 10
done
aws ssm get-command-invocation --region "$REGION" --command-id "$ID" --instance-id "$INSTANCE" \
  --query '[StandardOutputContent,StandardErrorContent]' --output text | tail -15
echo "Redeploy status: $S"
[ "$S" = Success ]
