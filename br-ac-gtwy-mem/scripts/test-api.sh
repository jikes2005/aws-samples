#!/usr/bin/env bash
# Call the private customer API directly. Run from a host INSIDE the VPC (EC2 / SSM session):
# the API is private and only accepts requests through the execute-api VPC endpoint.
#
# The API uses IAM auth, so requests are SigV4-signed with your current AWS credentials.
# Those credentials need execute-api:Invoke on the API (the tool Lambda's role has it).
# Needs curl 7.75+ (for --aws-sigv4) and AWS CLI v2.
set -euo pipefail
PROJECT="${PROJECT:-agentcore-poc}"
REGION="${AWS_REGION:-ca-central-1}"

BASE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$PROJECT" \
  --query "Stacks[0].Outputs[?OutputKey=='CustomerApiBaseUrl'].OutputValue" --output text)

# Export the current credentials (works for profiles, SSO and instance roles)
eval "$(aws configure export-credentials --format env)"
SIGN=(--aws-sigv4 "aws:amz:${REGION}:execute-api" --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}")
[[ -n "${AWS_SESSION_TOKEN:-}" ]] && SIGN+=(-H "x-amz-security-token: ${AWS_SESSION_TOKEN}")

echo "--- GET /customers (unsigned, expect 403)"; curl -s "$BASE/customers"; echo
echo "--- GET /customers";              curl -s "${SIGN[@]}" "$BASE/customers" | python3 -m json.tool
echo "--- GET /customers?province=ON";  curl -s "${SIGN[@]}" "$BASE/customers?province=ON" | python3 -m json.tool
echo "--- GET /customers/C-1001";       curl -s "${SIGN[@]}" "$BASE/customers/C-1001" | python3 -m json.tool
echo "--- POST /customers"
curl -s "${SIGN[@]}" -X POST -H 'Content-Type: application/json' "$BASE/customers" \
  -d "{\"firstName\":\"Erin\",\"lastName\":\"Park\",\"email\":\"erin.park+$RANDOM@example.com\",\"city\":\"Halifax\",\"province\":\"NS\"}" \
  | python3 -m json.tool
