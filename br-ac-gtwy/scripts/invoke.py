"""Invoke the deployed agent.

python scripts/invoke.py "Show me customer C-1002"
python scripts/invoke.py "Onboard Dana Lee, dana.lee@example.com, Ottawa ON, retail"
"""
import json
import os
import sys
import uuid

import boto3

STACK = os.environ.get("PROJECT", "agentcore-poc")
REGION = boto3.Session().region_name or "us-east-1"

cfn = boto3.client("cloudformation", region_name=REGION)
outputs = cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
runtime_arn = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "AgentRuntimeArn")

prompt = " ".join(sys.argv[1:]) or "List all active customers in Ontario."
client = boto3.client("bedrock-agentcore", region_name=REGION)
resp = client.invoke_agent_runtime(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId=str(uuid.uuid4()) + "-poc",  # must be >= 33 chars
    payload=json.dumps({"prompt": prompt}).encode(),
)
body = resp["response"].read().decode()
try:
    print(json.loads(body)["result"])
except (ValueError, KeyError):
    print(body)
