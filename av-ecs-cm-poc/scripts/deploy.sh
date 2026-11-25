#!/usr/bin/env bash
# Deploy the base stack, build/push the image, then deploy the services stack.
# Usage:
#   VPC_ID=vpc-123 SUBNET_IDS=subnet-a,subnet-b CLIENT_CIDR=10.0.0.0/8 \
#   NAMESPACE_MODE=DNS ./scripts/deploy.sh
# Uses your existing VPC, subnets and VPC endpoints.
set -euo pipefail

REGION="${AWS_REGION:-ca-central-1}"
PROJECT="${PROJECT:-av-ecs-cm-poc}"
BASE_STACK="${BASE_STACK:-${PROJECT}-base}"
SVC_STACK="${SVC_STACK:-${PROJECT}-services}"
NAMESPACE_MODE="${NAMESPACE_MODE:-DNS}"
NAMESPACE_NAME="${NAMESPACE_NAME:-poc.internal}"
TAG="${TAG:-v1}"
: "${VPC_ID:?set VPC_ID}" "${SUBNET_IDS:?set SUBNET_IDS (comma separated)}" "${CLIENT_CIDR:?set CLIENT_CIDR}"

# Required tags (edit to your values)
TAGS="AppCatID=${APP_CAT_ID:-000000} ServiceNameTag=${SERVICE_NAME_TAG:-$PROJECT} SupportTeam=${SUPPORT_TEAM:-cloud-architecture} Environment=${ENVIRONMENT:-lab}"

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/3 base stack: $BASE_STACK"
aws cloudformation deploy --region "$REGION" --stack-name "$BASE_STACK" \
  --template-file "$HERE/../cfn/01-base.yaml" --capabilities CAPABILITY_IAM \
  --parameter-overrides ProjectName="$PROJECT" VpcId="$VPC_ID" \
    ClientCidr="$CLIENT_CIDR" NamespaceName="$NAMESPACE_NAME" NamespaceMode="$NAMESPACE_MODE" \
    $TAGS

echo "== 2/3 image"
AWS_REGION="$REGION" PROJECT="$PROJECT" BASE_STACK="$BASE_STACK" TAG="$TAG" "$HERE/build_push.sh"

echo "== 3/3 services stack: $SVC_STACK"
aws cloudformation deploy --region "$REGION" --stack-name "$SVC_STACK" \
  --template-file "$HERE/../cfn/02-services.yaml" \
  --parameter-overrides ProjectName="$PROJECT" BaseStackName="$BASE_STACK" \
    NamespaceMode="$NAMESPACE_MODE" SubnetIds="$SUBNET_IDS" ImageTag="$TAG" $TAGS

aws cloudformation describe-stacks --region "$REGION" --stack-name "$SVC_STACK" \
  --query "Stacks[0].Outputs" --output table
