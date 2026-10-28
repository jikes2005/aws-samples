"""br-ac-react-chat API - REST backend for the React chat UI.

Runs on ECS Fargate behind API Gateway (private REST API -> VPC link -> NLB). Calls the
AgentCore Runtime agent (streaming) and reads chat history from its AgentCore Memory.

LAB VERSION - NO SIGN-IN: the caller's name comes from the X-Chat-User header the UI sends.
Anyone who can reach the API can send any name, so use demo data only.

Endpoints (all under /api):
  GET  /api/health                         health check (NLB)
  GET  /api/chats                          the user's recent chats   [X-Chat-User]
  GET  /api/chats/{chat_id}                one chat's messages       [X-Chat-User]
  POST /api/chats/{chat_id}/messages       {"prompt": "..."} -> server-sent events:
       data: {"type": "text", "text": ...} | {"type": "tool", "name": ...}
             | {"type": "done", "toolsUsed": [...]} | {"type": "error", "error": ...}
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "ca-central-1")
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
MEMORY_ID = os.environ["MEMORY_ID"]
# Origins the browser may call from (the UI's https:// URLs), comma-separated; "*" allows any
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MAX_CHATS = 20
CHAT_ID = re.compile(r"^chat-[a-zA-Z0-9-]{28,90}$")      # runtime session IDs must be >= 33 chars

# Agent turns with tool calls can take a while; don't retry (an invocation isn't idempotent).
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=Config(read_timeout=300, connect_timeout=10, retries={"total_max_attempts": 1}))

app = FastAPI(title="br-ac-react-chat API", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "OPTIONS"],
                   allow_headers=["Content-Type", "X-Chat-User"], max_age=600)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def actor_id(user):
    """'Jane Doe' -> 'jane-doe' (what AgentCore Memory accepts). 400 if missing."""
    value = re.sub(r"[^a-z0-9_-]+", "-", (user or "").strip().lower()).strip("-_")[:64]
    if not value:
        raise HTTPException(400, "Send your name in the X-Chat-User header")
    return value


def check_chat_id(chat_id):
    if not CHAT_ID.match(chat_id):
        raise HTTPException(400, "Chat IDs look like chat-<uuid>")
    return chat_id


def list_events(actor, session_id):
    events, token = [], None
    while True:
        kwargs = {"memoryId": MEMORY_ID, "actorId": actor, "sessionId": session_id,
                  "includePayloads": True, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = agentcore.list_events(**kwargs)
        events += page.get("events", [])
        token = page.get("nextToken")
        if not token:
            return sorted(events, key=lambda e: (e["eventTimestamp"], e["eventId"]))


def event_messages(event):
    """Strands messages stored in one memory event (other events hold session/agent state)."""
    out = []
    for item in event.get("payload", []):
        try:
            if "conversational" in item:
                out.append(json.loads(item["conversational"]["content"]["text"])["message"])
            elif "blob" in item:
                blob = json.loads(item["blob"]) if isinstance(item["blob"], str) else item["blob"]
                if isinstance(blob, list) and len(blob) == 2:
                    out.append(json.loads(blob[0])["message"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def to_chat(message):
    """Strands message -> {"role", "content"} for display, or None for tool traffic."""
    texts = [b["text"] for b in message.get("content", [])
             if "text" in b and not b["text"].startswith("<user_context>")]   # injected long-term memory
    text = "\n".join(t for t in texts if t.strip())
    return {"role": message["role"], "content": text} if text else None


def load_chat(actor, session_id):
    return [m for e in list_events(actor, session_id) for msg in event_messages(e) if (m := to_chat(msg))]


_titles = {}   # (actor, session) -> title; a chat's first question never changes


def chat_title(actor, session_id):
    key = (actor, session_id)
    if key not in _titles:
        first = next((m["content"] for m in load_chat(actor, session_id) if m["role"] == "user"), None)
        if not first:
            return None
        _titles[key] = first[:60]
    return _titles[key]


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value


def sse(event):
    return f"data: {json.dumps(event)}\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/chats")
def chats(x_chat_user: str = Header(None)):
    actor = actor_id(x_chat_user)
    sessions, token = [], None
    while True:
        kwargs = {"memoryId": MEMORY_ID, "actorId": actor, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = agentcore.list_sessions(**kwargs)
        sessions += page.get("sessionSummaries", [])
        token = page.get("nextToken")
        if not token:
            break
    sessions.sort(key=lambda s: s.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out = []
    for s in sessions[:MAX_CHATS]:
        title = chat_title(actor, s["sessionId"])
        if title:   # skip sessions with no conversation yet
            out.append({"id": s["sessionId"], "title": title, "createdAt": iso(s.get("createdAt"))})
    return {"user": actor, "chats": out}


@app.get("/api/chats/{chat_id}")
def chat(chat_id: str, x_chat_user: str = Header(None)):
    actor = actor_id(x_chat_user)
    return {"id": check_chat_id(chat_id), "messages": load_chat(actor, chat_id)}


class NewMessage(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: str, body: NewMessage, x_chat_user: str = Header(None)):
    actor = actor_id(x_chat_user)
    check_chat_id(chat_id)

    def events():
        started = time.time()
        yield ": stream open\n\n"          # comment line - sends the headers straight away
        try:
            resp = agentcore.invoke_agent_runtime(
                agentRuntimeArn=AGENT_RUNTIME_ARN,
                runtimeSessionId=chat_id,
                payload=json.dumps({"prompt": body.prompt, "actor_id": actor, "stream": True}).encode(),
            )
            stream = resp["response"]
            if "text/event-stream" not in resp.get("contentType", ""):
                data = json.loads(stream.read())          # older agent build without streaming
                if "error" in data:
                    yield sse({"type": "error", "error": str(data["error"])})
                    return
                yield sse({"type": "text", "text": data.get("result", "")})
                yield sse({"type": "done", "toolsUsed": [n.split("___")[-1] for n in data.get("tools_used") or {}]})
                return
            for line in stream.iter_lines():
                line = line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get("type")
                if kind == "text":
                    yield sse({"type": "text", "text": event.get("text", "")})
                elif kind == "tool":
                    yield sse({"type": "tool", "name": event.get("name", "").split("___")[-1]})
                elif kind == "done":
                    tools = [n.split("___")[-1] for n in event.get("tools_used") or {}]
                    yield sse({"type": "done", "toolsUsed": tools})
                elif kind == "error" or "error" in event:
                    yield sse({"type": "error", "error": str(event.get("error"))})
        except Exception as e:
            log.exception("Agent call failed (chat %s)", chat_id)
            yield sse({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            log.info("chat=%s user=%s took %.1fs", chat_id, actor, time.time() - started)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
