#!/usr/bin/env bash
# Run from a host INSIDE the VPC (EC2 / SSM session) - the API is private and the
# token comes from the cognito-idp VPC endpoint. Needs AWS CLI v2 recent enough to
# have `cognito-idp get-client-token` and IAM permission for DescribeUserPoolClient.
set -euo pipefail
PROJECT="${PROJECT:-agentcore-poc}"
REGION="${AWS_REGION:-ca-central-1}"

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BASE=$(out CustomerApiBaseUrl); POOL=$(out UserPoolId); CLIENT=$(out ToolsAppClientId)

SECRET=$(aws cognito-idp describe-user-pool-client --region "$REGION" \
  --user-pool-id "$POOL" --client-id "$CLIENT" --query UserPoolClient.ClientSecret --output text)
REQ=$(mktemp); chmod 600 "$REQ"; trap 'rm -f "$REQ"' EXIT
printf '{"ClientId":"%s","Secret":"%s"}' "$CLIENT" "$SECRET" > "$REQ"
TOKEN=$(aws cognito-idp get-client-token --region "$REGION" --no-sign-request \
  --cli-input-json "file://$REQ" --query ClientAuthenticationResult.AccessToken --output text)
AUTH=(-H "Authorization: Bearer $TOKEN")

echo "--- GET /customers (no token, expect 401)"; curl -s "$BASE/customers"; echo
echo "--- GET /customers";              curl -s "${AUTH[@]}" "$BASE/customers" | python3 -m json.tool
echo "--- GET /customers?province=ON";  curl -s "${AUTH[@]}" "$BASE/customers?province=ON" | python3 -m json.tool
echo "--- GET /customers/C-1001";       curl -s "${AUTH[@]}" "$BASE/customers/C-1001" | python3 -m json.tool
echo "--- POST /customers"
curl -s -X POST "${AUTH[@]}" -H 'Content-Type: application/json' "$BASE/customers" \
  -d "{\"firstName\":\"Erin\",\"lastName\":\"Park\",\"email\":\"erin.park+$RANDOM@example.com\",\"city\":\"Halifax\",\"province\":\"NS\"}" \
  | python3 -m json.tool
