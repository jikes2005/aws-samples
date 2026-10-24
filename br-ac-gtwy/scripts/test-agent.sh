#!/usr/bin/env bash
# Invoke the agent test Lambda.
#   ./scripts/test-agent.sh                         # built-in test suite (4 checks)
#   ./scripts/test-agent.sh "Show me customer C-1003"   # one question
set -euo pipefail
PROJECT="${PROJECT:-agentcore-poc}"
REGION="${AWS_REGION:-us-east-1}"
FN="$PROJECT-agent-test"
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT

if [[ $# -gt 0 ]]; then
  PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"prompt": " ".join(sys.argv[1:])}))' "$@")
else
  PAYLOAD='{}'
fi

aws lambda invoke --region "$REGION" --function-name "$FN" \
  --cli-binary-format raw-in-base64-out --cli-read-timeout 620 \
  --payload "$PAYLOAD" "$OUT" >/dev/null

python3 - "$OUT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
if "errorMessage" in r:
    print("Lambda error:", r["errorMessage"]); sys.exit(1)
for x in r["results"]:
    print(f"\n[{x['status']}] {x['name']}  ({x.get('seconds', '-')}s)")
    print("  Q:", x.get("prompt", ""))
    print("  A:", x.get("answer") or x.get("error") or x.get("reason"))
print("\nSummary:", r["summary"])
sys.exit(1 if any(k in r["summary"] for k in ("FAIL", "ERROR")) else 0)
PY
