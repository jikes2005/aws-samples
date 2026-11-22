#!/usr/bin/env bash
# Turns on CloudWatch Transaction Search for this account and Region - a one-time, ACCOUNT-WIDE
# setting that AgentCore / GenAI Observability needs before any spans appear. Safe to re-run.
#
#   1. lets X-Ray write spans into CloudWatch Logs (log resource policy)
#   2. sends X-Ray trace segments to CloudWatch Logs (Transaction Search)
#   3. sets how many spans are indexed as searchable trace summaries (default 100% for a POC)
#
# Usage: AWS_REGION=ca-central-1 ./scripts/enable-transaction-search.sh [indexing-percent]
# Cost: spans are ingested as CloudWatch Logs; indexing above the free 1% is billed - lower it later.
set -euo pipefail

REGION="${AWS_REGION:-ca-central-1}"
PERCENT="${1:-100}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
PARTITION=$(aws sts get-caller-identity --query Arn --output text | cut -d: -f2)

echo "Account $ACCOUNT, Region $REGION"
CURRENT=$(aws xray get-trace-segment-destination --region "$REGION" --query "[Destination,Status]" --output text 2>/dev/null || echo "unknown")
echo "Current trace segment destination: $CURRENT"

if [[ "$CURRENT" != CloudWatchLogs* ]]; then
  POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"TransactionSearchXRayAccess","Effect":"Allow",
"Principal":{"Service":"xray.amazonaws.com"},"Action":"logs:PutLogEvents",
"Resource":["arn:${PARTITION}:logs:${REGION}:${ACCOUNT}:log-group:aws/spans:*",
"arn:${PARTITION}:logs:${REGION}:${ACCOUNT}:log-group:/aws/application-signals/data:*"],
"Condition":{"ArnLike":{"aws:SourceArn":"arn:${PARTITION}:xray:${REGION}:${ACCOUNT}:*"},
"StringEquals":{"aws:SourceAccount":"${ACCOUNT}"}}}]}
EOF
)
  echo "==> log resource policy for X-Ray"
  aws logs put-resource-policy --region "$REGION" --policy-name TransactionSearchXRayAccess \
    --policy-document "$POLICY" --query "resourcePolicy.policyName" --output text
  echo "==> trace segments -> CloudWatch Logs"
  aws xray update-trace-segment-destination --region "$REGION" --destination CloudWatchLogs \
    --query "[Destination,Status]" --output text
else
  echo "Transaction Search is already on"
fi

echo "==> index $PERCENT% of spans as trace summaries"
aws xray update-indexing-rule --region "$REGION" --name Default \
  --rule "{\"Probabilistic\":{\"DesiredSamplingPercentage\":$PERCENT}}" \
  --query "IndexingRule.Rule.Probabilistic.DesiredSamplingPercentage" --output text

echo "Done. It can take a few minutes before the destination shows ACTIVE:"
echo "  aws xray get-trace-segment-destination --region $REGION"
