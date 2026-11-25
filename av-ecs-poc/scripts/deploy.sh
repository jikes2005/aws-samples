
set -euo pipefail
REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-av-ecs-poc}"
BASE_STACK="${PROJECT}-base"
SVC_STACK="${PROJECT}-services"
TAG="${TAG:-v1}"
: "${VPC_ID:?set VPC_ID}" "${SUBNET_IDS:?set SUBNET_IDS (comma separated)}" "${CLIENT_CIDR:?set CLIENT_CIDR}"
TAGS="AppCatID=${APP_CAT_ID:-000000} ServiceNameTag=${SERVICE_NAME_TAG:-$PROJECT} SupportTeam=${SUPPORT_TEAM:-cloud-architecture} Environment=${ENVIRONMENT:-lab}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1/3 base stack $BASE_STACK"
aws cloudformation deploy --region "$REGION" --stack-name "$BASE_STACK" \
  --template-file "$HERE/../cfn/01-base.yaml" --capabilities CAPABILITY_IAM \
  --parameter-overrides ProjectName="$PROJECT" VpcId="$VPC_ID" SubnetIds="$SUBNET_IDS" \
    ClientCidr="$CLIENT_CIDR" $TAGS

if [ -z "${SKIP_BUILD:-}" ]; then
  echo "== 2/3 image"
  AWS_REGION="$REGION" PROJECT="$PROJECT" TAG="$TAG" "$HERE/build_push.sh"
fi

echo "== 3/3 services stack $SVC_STACK"
aws cloudformation deploy --region "$REGION" --stack-name "$SVC_STACK" \
  --template-file "$HERE/../cfn/02-services.yaml" \
  --parameter-overrides ProjectName="$PROJECT" BaseStackName="$BASE_STACK" ImageTag="$TAG" $TAGS

aws cloudformation describe-stacks --region "$REGION" --stack-name "$SVC_STACK" --query "Stacks[0].Outputs" --output table
