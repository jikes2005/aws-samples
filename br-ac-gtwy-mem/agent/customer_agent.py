import json
import logging
import os
import re
import uuid

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

try:  # Strands helper that works with both mcp 1.x and mcp 2.x
    from strands.tools.mcp._compat import streamable_http_transport
except ImportError:  # older Strands (mcp 1.x only)
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_transport

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("customer_agent")
MAX_LOG_CHARS = 2000  # truncate long tool results in the log

# Settings come from the runtime's environment variables (set by the CloudFormation stack)
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "ca-central-1")
GATEWAY_URL = os.environ["GATEWAY_URL"]
MEMORY_ID = os.environ["MEMORY_ID"]

# Long-term memory namespaces searched on every prompt (must match the strategies in the template).
# {actorId} is filled in per user, so each user only sees their own memories.
MEMORY_NAMESPACES = {
    "/facts/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.3),
    "/preferences/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.3),
}

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
    "Before creating a customer, make sure you have first name, last name and email. "
    "A <user_context> block, when present, holds what you remember about this user from earlier "
    "conversations; use it, but always get current customer data from the tools."
)

log.info("Agent configured: model=%s region=%s gateway=%s memory=%s", MODEL_ID, REGION, GATEWAY_URL, MEMORY_ID)


def memory_id_value(value, default):
    """Keep actor and session IDs to letters, digits, '-' and '_' (what AgentCore Memory accepts)."""
    value = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value or "")).strip("-_")
    return value[:100] or default


def log_tool_calls(messages):
    """Log each MCP tool call the agent made and the result the gateway returned."""
    for message in messages:
        for block in message.get("content", []):
            if "toolUse" in block:
                call = block["toolUse"]
                log.info("MCP call: %s input=%s", call["name"], call.get("input"))
            elif "toolResult" in block:
                res = block["toolResult"]
                text = " ".join(one_line(c.get("text", c.get("json", ""))) for c in res.get("content", []))
                log.info("MCP result (%s): %s", res.get("status"), text[:MAX_LOG_CHARS])


def one_line(value):
    """Compact a tool result onto one line (CloudWatch logs each line as a separate event)."""
    try:
        return json.dumps(json.loads(value) if isinstance(value, str) else value, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).replace("\n", " ")


@app.entrypoint
def invoke(payload, context=None):
    log.info("Received payload: %s", payload)

    if isinstance(payload, str):
        payload = {"prompt": payload}
    prompt = (payload.get("prompt") or payload.get("input")
              or payload.get("message") or "Hello!")

    # Memory is scoped by actor (who is talking) and session (which conversation).
    # The session is the runtime session ID, so calls that reuse it continue the conversation.
    actor_id = memory_id_value(payload.get("actor_id") or payload.get("user_id"), "default-user")
    session_id = memory_id_value(getattr(context, "session_id", None) or payload.get("session_id"),
                                 f"session-{uuid.uuid4()}")
    log.info("Memory: actor=%s session=%s", actor_id, session_id)

    memory_config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
        retrieval_config=MEMORY_NAMESPACES,   # long-term memory, injected as <user_context>
    )

    # Saves every message straight away (no batching), so nothing needs flushing at the end.
    memory = AgentCoreMemorySessionManager(memory_config, region_name=REGION)

    # Tools come from AgentCore Gateway (MCP). Open a connection for this request.
    with MCPClient(lambda: streamable_http_transport(GATEWAY_URL, auth=GatewayAuth())) as gateway:
        tools = gateway.list_tools_sync()
        log.info("Gateway tools: %s", [t.tool_name for t in tools])

        # The session manager loads earlier turns of this session (short-term memory)
        # and saves each new message back to AgentCore Memory as it is added.
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools,
                      session_manager=memory, callback_handler=None)
        history = len(agent.messages)
        log.info("Short-term memory: %d earlier message(s) restored", history)

        result = agent(prompt)

    new_messages = agent.messages[history:]
    log_long_term_memory(new_messages)
    log_tool_calls(new_messages)

    tools_used = {name: m.call_count for name, m in result.metrics.tool_metrics.items()}
    log.info("Tools used: %s", tools_used)

    return {"result": str(result), "tools_used": tools_used, "prompt_seen": prompt,
            "actor_id": actor_id, "session_id": session_id, "messages_restored": history}


def log_long_term_memory(messages):
    """Log the long-term memories the session manager added to this prompt, if any."""
    for block in (messages[0].get("content", []) if messages else []):
        text = block.get("text", "")
        if text.startswith("<user_context>"):
            log.info("Long-term memory used: %s", one_line(text)[:MAX_LOG_CHARS])
            return
    log.info("Long-term memory used: none found for this prompt")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)   # listen on all interfaces so the runtime's health check can reach it
