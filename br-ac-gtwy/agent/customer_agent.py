"""Strands agent hosted on AgentCore Runtime.

Tools are discovered at request time from AgentCore Gateway (MCP over
streamable HTTP). The gateway uses AWS_IAM inbound auth, so every MCP request
is SigV4-signed with the runtime role's credentials.
"""
import logging
import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("customer-agent")

GATEWAY_URL = os.environ["GATEWAY_URL"]
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "us-east-1")

SYSTEM_PROMPT = """You are a customer-service assistant for a Canadian bank (demo data only).
Use the customer tools to look up, list, or create customers. Never invent customer
data - if a tool returns an error, 404 or 409 (duplicate email), say so plainly.
Before creating a customer, confirm you have first name, last name and email.
Keep answers short and factual."""

session = boto3.Session(region_name=REGION)
app = BedrockAgentCoreApp()


class SigV4HttpxAuth(httpx.Auth):
    """Signs each outgoing httpx request for the bedrock-agentcore service."""

    requires_request_body = True

    def __init__(self, service: str = "bedrock-agentcore"):
        self.service = service

    def auth_flow(self, request: httpx.Request):
        creds = session.get_credentials().get_frozen_credentials()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("connection", "accept-encoding")}
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(creds, self.service, REGION).add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


def _mcp_client() -> MCPClient:
    return MCPClient(lambda: streamablehttp_client(GATEWAY_URL, auth=SigV4HttpxAuth()))


model = BedrockModel(model_id=MODEL_ID, region_name=REGION, temperature=0.2)


@app.entrypoint
def invoke(payload: dict) -> dict:
    prompt = payload.get("prompt") or "List all active customers."
    log.info("prompt=%s", prompt)
    with _mcp_client() as mcp:
        tools = mcp.list_tools_sync()
        log.info("tools=%s", [t.tool_name for t in tools])
        agent = Agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)
        result = agent(prompt)
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
