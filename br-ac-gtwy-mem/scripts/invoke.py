"""Invoke the deployed agent.

python scripts/invoke.py "Show me customer C-1002"
python scripts/invoke.py "Onboard Dana Lee, dana.lee@example.com, Ottawa ON, retail"

Memory:
  ACTOR_ID   - who is talking (long-term memory is kept per actor). Default: demo-user
  SESSION_ID - reuse the session ID printed by a previous call to continue that
               conversation (short-term memory). Default: a new session each call.

  SESSION_ID=<id from previous run> python scripts/invoke.py "Which of those are in Toronto?"
"""
import json
import os
import sys
import uuid

import boto3

STACK = os.environ.get("PROJECT", "agentcore-poc")
REGION = boto3.Session().region_name or "ca-central-1"
ACTOR_ID = os.environ.get("ACTOR_ID", "demo-user")
SESSION_ID = os.environ.get("SESSION_ID") or f"{uuid.uuid4()}-poc"   # must be >= 33 chars

cfn = boto3.client("cloudformation", region_name=REGION)
outputs = cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
runtime_arn = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "AgentRuntimeArn")

prompt = " ".join(sys.argv[1:]) or "List all active customers in Ontario."
client = boto3.client("bedrock-agentcore", region_name=REGION)
resp = client.invoke_agent_runtime(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId=SESSION_ID,
    payload=json.dumps({"prompt": prompt, "actor_id": ACTOR_ID}).encode(),
)
body = resp["response"].read().decode()
try:
    data = json.loads(body)
    print(data["result"])
    print(f"\n[actor={data.get('actor_id')} session={SESSION_ID} "
          f"earlier messages restored={data.get('messages_restored')}]")
except (ValueError, KeyError):
    print(body)
