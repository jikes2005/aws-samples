#!/usr/bin/env bash
# Deploys / updates the br-ac-chat stack next to an existing agent stack. The image is
# built first in app/build_image.ipynb (which also creates the ECR repository) - pass the
# IMAGE_URI and CPU_ARCHITECTURE it prints. On later runs they're optional: leaving them
# unset keeps the stack's current image.
#
# Usage: export the variables below, then ./scripts/deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-br-ac-chat}"
AGENT_STACK="${AGENT_STACK:-agentcore-poc}"
REGION="${AWS_REGION:-ca-central-1}"
VPC_ID="${VPC_ID:?set VPC_ID}"
SUBNET_IDS="${SUBNET_IDS:?set SUBNET_IDS, comma-separated private subnets in 2+ AZs}"
CLIENT_CIDR="${CLIENT_CIDR:?set CLIENT_CIDR - the address range users browse from, e.g. 10.0.0.0/8}"
EXTRA_TLS_NAMES="${EXTRA_TLS_NAMES:-}"      # optional extra certificate names/IPs, comma-separated
KMS_KEY_ARN="${KMS_KEY_ARN:-}"              # blank = stack creates its own CMK
IMAGE_URI="${IMAGE_URI:-}"                  # from build_image.ipynb (required the first time)
CPU_ARCHITECTURE="${CPU_ARCHITECTURE:-}"    # X86_64 or ARM64, from build_image.ipynb
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

echo "==> 1/2 read the agent stack ($AGENT_STACK)"
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
if [[ -n "$IMAGE_URI" ]]; then
  IMAGE_PARAMS+=(ImageUri="$IMAGE_URI" CpuArchitecture="${CPU_ARCHITECTURE:-X86_64}")
elif ! aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: set IMAGE_URI (and CPU_ARCHITECTURE) - run app/build_image.ipynb first; it prints both"; exit 1
else
  echo "    image  : unchanged (set IMAGE_URI to change it)"
fi

echo "==> 2/2 deploy stack $PROJECT"
aws cloudformation deploy --region "$REGION" \
  --stack-name "$PROJECT" --template-file "$ROOT/cfn/br-ac-chat.yaml" \
  --parameter-overrides ProjectName="$PROJECT" \
    VpcId="$VPC_ID" PrivateSubnetIds="$SUBNET_IDS" ClientCidr="$CLIENT_CIDR" \
    AgentRuntimeArn="$AGENT_RUNTIME_ARN" MemoryId="$MEMORY_ID" AgentKmsKeyArn="$AGENT_KMS_KEY_ARN" \
    ExtraTlsNames="$EXTRA_TLS_NAMES" ${IMAGE_PARAMS[@]+"${IMAGE_PARAMS[@]}"} \
    ExistingKmsKeyArn="$KMS_KEY_ARN" \
    AppCatID="$APP_CAT_ID" SupportTeam="$SUPPORT_TEAM" ServiceName="$SERVICE_NAME" Environment="$ENVIRONMENT" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset

aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" \
  --query "Stacks[0].Outputs" --output table
