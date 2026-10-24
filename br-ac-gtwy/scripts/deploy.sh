#!/usr/bin/env bash
# Deploys the AgentCore MCP POC (one CloudFormation stack) into an EXISTING
# VPC / subnets / VPC endpoints. The only step outside CloudFormation is
# packaging the agent code and uploading it to S3.
#
# Usage: export the variables below, then ./scripts/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-agentcore-mcp-poc}"
REGION="${AWS_REGION:-us-east-1}"
VPC_ID="${VPC_ID:?set VPC_ID}"
SUBNET_IDS="${SUBNET_IDS:?set SUBNET_IDS, comma-separated private subnets}"
EXECUTE_API_VPCE="${EXECUTE_API_VPCE:?set EXECUTE_API_VPCE (existing execute-api endpoint, vpce-...)}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:?set ARTIFACT_BUCKET (existing S3 bucket)}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
STAGE="${STAGE:-dev}"
HELPER_IN_VPC="${HELPER_IN_VPC:-true}"   # false if the VPC has no S3 gateway endpoint
KMS_KEY_ARN="${KMS_KEY_ARN:-}"           # blank = stack creates its own CMK
APPLY_COMMON_WAF="${APPLY_COMMON_WAF:-true}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-35}"
CREATE_APIGW_ACCOUNT_ROLE="${CREATE_APIGW_ACCOUNT_ROLE:-false}"  # true only if the account has no API GW CloudWatch role

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
STACK="$PROJECT"
CODE_KEY="$PROJECT/agent-$(date +%Y%m%d%H%M%S).zip"   # new key each deploy forces a runtime update

if [[ "$CREATE_APIGW_ACCOUNT_ROLE" != "true" ]]; then
  CW_ROLE=$(aws apigateway get-account --region "$REGION" --query cloudwatchRoleArn --output text 2>/dev/null || true)
  if [[ -z "$CW_ROLE" || "$CW_ROLE" == "None" ]]; then
    echo "ERROR: API Gateway has no CloudWatch Logs role in $REGION, so stage access logging will fail."
    echo "       Re-run with CREATE_APIGW_ACCOUNT_ROLE=true (sets the account-level role) or ask your platform team."
    exit 1
  fi
fi

echo "==> 1/3 package agent for linux/arm64 (AgentCore Runtime)"
rm -rf "$BUILD" && mkdir -p "$BUILD/pkg"
pip install -q -r "$ROOT/agent/requirements.txt" --target "$BUILD/pkg" \
  --platform manylinux2014_aarch64 --python-version 3.12 --implementation cp --only-binary=:all:
cp "$ROOT/agent/main.py" "$BUILD/pkg/"
(cd "$BUILD/pkg" && zip -qr "$BUILD/agent.zip" .)
aws s3 cp --region "$REGION" "$BUILD/agent.zip" "s3://$ARTIFACT_BUCKET/$CODE_KEY"

echo "==> 2/3 deploy stack $STACK"
aws cloudformation deploy --region "$REGION" \
  --stack-name "$STACK" --template-file "$ROOT/cfn/agentcore-mcp-poc.yaml" \
  --parameter-overrides ProjectName="$PROJECT" StageName="$STAGE" \
    VpcId="$VPC_ID" PrivateSubnetIds="$SUBNET_IDS" ExecuteApiVpceId="$EXECUTE_API_VPCE" \
    HelperFunctionsInVpc="$HELPER_IN_VPC" \
    ExistingKmsKeyArn="$KMS_KEY_ARN" ApplyCommonWaf="$APPLY_COMMON_WAF" \
    BackupRetentionDays="$BACKUP_RETENTION_DAYS" CreateApiGatewayAccountRole="$CREATE_APIGW_ACCOUNT_ROLE" \
    ArtifactBucket="$ARTIFACT_BUCKET" AgentCodeKey="$CODE_KEY" ModelId="$MODEL_ID" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset

echo "==> 3/3 redeploy API stage (CloudFormation doesn't redeploy a stage when methods change)"
API_ID=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='CustomerApiId'].OutputValue" --output text)
aws apigateway create-deployment --region "$REGION" --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --description "deploy.sh $(date -u +%FT%TZ)" >/dev/null

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs" --output table
