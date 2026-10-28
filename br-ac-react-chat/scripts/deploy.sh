#!/usr/bin/env bash
# Deploys / updates the br-ac-react-chat stack next to an existing agent stack. Build the
# images first with build_images.ipynb (which also creates the ECR repositories) and pass the
# UI_IMAGE_URI, API_IMAGE_URI and CPU_ARCHITECTURE it prints. On later runs they're optional:
# leaving them unset keeps the stack's current images.
#
# Usage: export the variables below, then ./scripts/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-br-ac-react-chat}"
AGENT_STACK="${AGENT_STACK:-agentcore-poc}"
REGION="${AWS_REGION:-ca-central-1}"
VPC_ID="${VPC_ID:?set VPC_ID}"
SUBNET_IDS="${SUBNET_IDS:?set SUBNET_IDS, comma-separated private subnets in 2+ AZs}"
CLIENT_CIDR="${CLIENT_CIDR:?set CLIENT_CIDR - the address range users browse from, e.g. 10.0.0.0/8}"
EXECUTE_API_VPCE="${EXECUTE_API_VPCE:?set EXECUTE_API_VPCE (existing execute-api endpoint, vpce-...)}"
UI_IMAGE_URI="${UI_IMAGE_URI:-}"            # from build_images.ipynb (required the first time)
API_IMAGE_URI="${API_IMAGE_URI:-}"
CPU_ARCHITECTURE="${CPU_ARCHITECTURE:-}"    # X86_64 or ARM64, from build_images.ipynb
STAGE="${STAGE:-dev}"
EXTRA_TLS_NAMES="${EXTRA_TLS_NAMES:-}"      # optional extra names/IPs users browse the UI by, comma-separated
KMS_KEY_ARN="${KMS_KEY_ARN:-}"              # blank = stack creates its own CMK
# Required tags (defaults match the template)
APP_CAT_ID="${APP_CAT_ID:-12345}"
SUPPORT_TEAM="${SUPPORT_TEAM:-CLOUD}"
SERVICE_NAME="${SERVICE_NAME:-AgentCore POC}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

agent_output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$AGENT_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

echo "==> 1/3 checks"
CW_ROLE=$(aws apigateway get-account --region "$REGION" --query cloudwatchRoleArn --output text 2>/dev/null || true)
if [[ -z "$CW_ROLE" || "$CW_ROLE" == "None" ]]; then
  echo "ERROR: API Gateway has no CloudWatch Logs role in $REGION, so the stage's logging will fail."
  echo "       Set it once per account/region (the agent stack can: CREATE_APIGW_ACCOUNT_ROLE=true)."
  exit 1
fi
AGENT_RUNTIME_ARN=$(agent_output AgentRuntimeArn)
MEMORY_ID=$(agent_output MemoryId)
AGENT_KMS_KEY_ARN=$(agent_output KmsKeyArn)
for v in AGENT_RUNTIME_ARN MEMORY_ID AGENT_KMS_KEY_ARN; do
  if [[ -z "${!v}" || "${!v}" == "None" ]]; then
    echo "ERROR: $v not found in $AGENT_STACK outputs - deploy the agent stack (with AgentCore Memory) first"; exit 1
  fi
done
echo "    runtime: $AGENT_RUNTIME_ARN"
echo "    memory : $MEMORY_ID"

IMAGE_PARAMS=()
if [[ -n "$UI_IMAGE_URI" || -n "$API_IMAGE_URI" ]]; then
  [[ -n "$UI_IMAGE_URI" && -n "$API_IMAGE_URI" ]] || { echo "ERROR: set both UI_IMAGE_URI and API_IMAGE_URI"; exit 1; }
  IMAGE_PARAMS+=(UiImageUri="$UI_IMAGE_URI" ApiImageUri="$API_IMAGE_URI" CpuArchitecture="${CPU_ARCHITECTURE:-X86_64}")
elif ! aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: set UI_IMAGE_URI, API_IMAGE_URI and CPU_ARCHITECTURE - run build_images.ipynb first; it prints them"; exit 1
else
  echo "    images : unchanged (set UI_IMAGE_URI and API_IMAGE_URI to change them)"
fi

echo "==> 2/3 deploy stack $PROJECT"
aws cloudformation deploy --region "$REGION" \
  --stack-name "$PROJECT" --template-file "$ROOT/cfn/br-ac-react-chat.yaml" \
  --parameter-overrides ProjectName="$PROJECT" StageName="$STAGE" \
    VpcId="$VPC_ID" PrivateSubnetIds="$SUBNET_IDS" ClientCidr="$CLIENT_CIDR" ExecuteApiVpceId="$EXECUTE_API_VPCE" \
    AgentRuntimeArn="$AGENT_RUNTIME_ARN" MemoryId="$MEMORY_ID" AgentKmsKeyArn="$AGENT_KMS_KEY_ARN" \
    ExtraTlsNames="$EXTRA_TLS_NAMES" ${IMAGE_PARAMS[@]+"${IMAGE_PARAMS[@]}"} \
    ExistingKmsKeyArn="$KMS_KEY_ARN" \
    AppCatID="$APP_CAT_ID" SupportTeam="$SUPPORT_TEAM" ServiceName="$SERVICE_NAME" Environment="$ENVIRONMENT" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset

echo "==> 3/3 redeploy the API stage (CloudFormation doesn't redeploy a stage when methods change)"
API_ID=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" \
  --query "Stacks[0].Outputs[?OutputKey=='RestApiId'].OutputValue" --output text)
aws apigateway create-deployment --region "$REGION" --rest-api-id "$API_ID" --stage-name "$STAGE" \
  --description "deploy.sh $(date -u +%FT%TZ)" >/dev/null

aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" \
  --query "Stacks[0].Outputs" --output table
