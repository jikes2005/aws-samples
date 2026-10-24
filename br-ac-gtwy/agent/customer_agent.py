import logging
import os

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

try:  # Strands helper that works with both mcp 1.x and mcp 2.x
    from strands.tools.mcp._compat import streamable_http_transport
except ImportError:  # older Strands (mcp 1.x only)
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_transport

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("customer_agent")

# Settings come from the runtime's environment variables (set by the CloudFormation stack)
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "ca-central-1")
GATEWAY_URL = os.environ["GATEWAY_URL"]

app = BedrockAgentCoreApp()
session = boto3.Session(region_name=REGION)


class GatewayAuth(httpx.Auth):
    """Signs MCP requests with the runtime role (the gateway uses AWS_IAM auth)."""

    requires_request_body = True

    def auth_flow(self, request):
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content,
                                 headers={"Content-Type": request.headers.get("Content-Type", "")})
        SigV4Auth(session.get_credentials(), "bedrock-agentcore", REGION).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.2,     # lower = more consistent, better for tool use
    max_tokens=2048,
)

SYSTEM_PROMPT = (
    "You are a customer-service assistant for a Canadian bank (demo data only). "
    "Use the customer tools to look up, list or create customers, and base your answer on the tool results. "
    "If a tool fails or returns 404 or 409 (duplicate email), say so rather than guessing. "
    "Before creating a customer, make sure you have first name, last name and email."
)

log.info("Agent configured: model=%s region=%s gateway=%s", MODEL_ID, REGION, GATEWAY_URL)


@app.entrypoint
def invoke(payload):
    log.info("Received payload: %s", payload)

    if isinstance(payload, str):
        prompt = payload
    else:
        prompt = (payload.get("prompt") or payload.get("input")
                  or payload.get("message") or "Hello!")

    # Tools come from AgentCore Gateway (MCP). Open a connection for this request.
    with MCPClient(lambda: streamable_http_transport(GATEWAY_URL, auth=GatewayAuth())) as gateway:
        tools = gateway.list_tools_sync()
        log.info("Gateway tools: %s", [t.tool_name for t in tools])

        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools, callback_handler=None)
        result = agent(prompt)

    tools_used = {name: m.call_count for name, m in result.metrics.tool_metrics.items()}
    log.info("Tools used: %s", tools_used)

    return {"result": str(result), "tools_used": tools_used, "prompt_seen": prompt}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)   # listen on all interfaces so the runtime's health check can reach it
