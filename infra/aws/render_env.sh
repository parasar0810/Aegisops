#!/bin/bash
# Render the app's .env on the EC2 instance from SSM Parameter Store (secrets) and instance
# metadata (public IP -> HTTPS hostname). Safe to re-run; used at first boot and on every redeploy.
#   bash infra/aws/render_env.sh <region> <ssm-prefix>
set -euo pipefail
REGION=${1:-ap-south-1}
PREFIX=${2:-/aegisops}
cd "$(dirname "$0")/../.."   # app root (contains docker-compose.yml)

IMDS=http://169.254.169.254/latest
TOKEN=$(curl -sf -X PUT "$IMDS/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IP=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" "$IMDS/meta-data/public-ipv4")
DOMAIN="${IP//./-}.sslip.io"   # free DNS name that resolves to this IP; lets Let's Encrypt issue a cert

umask 077
aws ssm get-parameters-by-path --region "$REGION" --path "$PREFIX/" --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text | while read -r name value; do
  echo "$(basename "$name")=$value"
done > .env.new
{
  echo "AEGIS_LLM_MODE=auto"
  echo "DOMAIN=$DOMAIN"
  echo "AEGIS_PUBLIC_URL=https://$DOMAIN"
  # Demo fault injection in ShopFront: token-protected, reachable only on the private Docker network.
  echo "SHOPFRONT_FAULTS_ENABLED=true"
} >> .env.new
mv .env.new .env
echo "Rendered .env for https://$DOMAIN ($(grep -c = .env) entries; GEMINI_API_KEY $(grep -q '^GEMINI_API_KEY=' .env && echo present || echo absent))"
