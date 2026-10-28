"""Customer Assistant - Streamlit chat UI for the AgentCore customer agent.

LAB VERSION - NO SIGN-IN. Users type a name, and that name scopes their chats and the
agent's long-term memory (actor_id). Anyone who can reach the app can type any name,
so use it with demo data only. Served over HTTPS with a self-signed certificate
(see entrypoint.py) behind an internal Network Load Balancer.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
import streamlit as st
from botocore.config import Config

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("chat")

REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION", "ca-central-1")
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
MEMORY_ID = os.environ["MEMORY_ID"]
MAX_CHATS = 15

# Agent turns with tool calls can take a while; don't retry (an invocation isn't idempotent).
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=Config(read_timeout=300, connect_timeout=10, retries={"total_max_attempts": 1}))


# ---------------------------------------------------------------------------
# Who is chatting (self-declared name - no authentication in this lab version)
# ---------------------------------------------------------------------------
def actor_id_for(name):
    """'Jane Doe' -> 'jane-doe' (letters, digits, '-' and '_' only, as AgentCore Memory expects)."""
    return re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")[:64]


def current_user():
    """The name in the URL (?user=jane) or the one typed on the welcome screen."""
    name = st.query_params.get("user", "")
    if actor_id_for(name):
        return {"id": actor_id_for(name), "name": name.strip()}

    st.title("Customer Assistant")
    st.caption("Lab version - no sign-in. Your name keeps your chats and the assistant's memory of you "
               "separate from other people's.")
    with st.form("who"):
        typed = st.text_input("Your name", placeholder="e.g. jane.doe")
        if st.form_submit_button("Start", type="primary") and actor_id_for(typed):
            st.query_params["user"] = typed.strip()      # kept in the URL, so a refresh or bookmark keeps it
            st.rerun()
    st.stop()


# ---------------------------------------------------------------------------
# Conversations (read from AgentCore Memory - the agent writes them)
# ---------------------------------------------------------------------------
def list_events(actor_id, session_id):
    events, token = [], None
    while True:
        kwargs = {"memoryId": MEMORY_ID, "actorId": actor_id, "sessionId": session_id,
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
    """Strands message -> (role, text) for display, or None for tool traffic."""
    texts = [b["text"] for b in message.get("content", [])
             if "text" in b and not b["text"].startswith("<user_context>")]   # injected long-term memory
    text = "\n".join(t for t in texts if t.strip())
    return (message["role"], text) if text else None


@st.cache_data(ttl=300, show_spinner=False)
def load_chat(actor_id, session_id):
    chat = []
    for event in list_events(actor_id, session_id):
        for message in event_messages(event):
            if (entry := to_chat(message)):
                chat.append(entry)
    return chat


@st.cache_data(ttl=30, show_spinner=False)
def list_chats(actor_id):
    sessions, token = [], None
    while True:
        kwargs = {"memoryId": MEMORY_ID, "actorId": actor_id, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = agentcore.list_sessions(**kwargs)
        sessions += page.get("sessionSummaries", [])
        token = page.get("nextToken")
        if not token:
            break
    sessions.sort(key=lambda s: s.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    chats = []
    for s in sessions[:MAX_CHATS]:
        first_user = next((text for role, text in load_chat(actor_id, s["sessionId"]) if role == "user"), None)
        if first_user:   # skip sessions with no conversation yet
            chats.append({"id": s["sessionId"], "title": first_user[:40], "created": s.get("createdAt")})
    return chats


# ---------------------------------------------------------------------------
# Calling the agent (streaming)
# ---------------------------------------------------------------------------
def agent_events(prompt, user, session_id):
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt, "actor_id": user["id"], "stream": True}).encode(),
    )
    body = resp["response"]
    if "text/event-stream" not in resp.get("contentType", ""):
        data = json.loads(body.read())      # older agent build without streaming
        if "error" in data:
            yield {"type": "error", "error": data["error"]}
            return
        yield {"type": "text", "text": data.get("result", "")}
        yield {"type": "done", **data}
        return
    for line in body.iter_lines():
        line = line.decode("utf-8").strip()
        if line.startswith("data:"):
            event = json.loads(line[5:].strip())
            if "error" in event and "type" not in event:   # runtime-level stream error
                event = {"type": "error", "error": event["error"]}
            yield event


def new_session_id():
    return f"chat-{uuid.uuid4()}"   # runtime session IDs must be at least 33 characters


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Customer Assistant", page_icon=":material/support_agent:", layout="centered")
user = current_user()

if st.session_state.get("user_id") != user["id"]:     # first visit, or the name in the URL changed
    st.session_state.clear()
    st.session_state.user_id = user["id"]

if "session_id" not in st.session_state:
    st.session_state.session_id = new_session_id()
    st.session_state.messages = []

with st.sidebar:
    st.markdown(f"**{user['name']}**")
    if st.button("Change name", icon=":material/logout:", width="stretch"):
        st.query_params.clear()
        st.session_state.clear()
        st.rerun()
    st.divider()
    if st.button("New chat", icon=":material/add:", type="primary", width="stretch"):
        st.session_state.session_id = new_session_id()
        st.session_state.messages = []
        st.rerun()

    st.caption("Recent chats")
    try:
        chats = list_chats(user["id"])
    except Exception as e:
        log.exception("Could not list chats")
        chats = []
        st.caption(f"Couldn't load history ({type(e).__name__})")
    for chat in chats:
        current = chat["id"] == st.session_state.session_id
        if st.button(chat["title"], key=chat["id"], width="stretch",
                     type="secondary", disabled=current):
            st.session_state.session_id = chat["id"]
            st.session_state.messages = [{"role": r, "content": t} for r, t in load_chat(user["id"], chat["id"])]
            st.rerun()

st.title("Customer Assistant")
if not st.session_state.messages:
    st.caption("Ask about customers - look one up, list by province or segment, or onboard a new customer. "
               "I remember your preferences between chats.")

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("tools"):
            st.caption(f"Tools used: {m['tools']}")

if prompt := st.chat_input("Ask about a customer..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.empty()
        done = {}

        def answer_chunks():
            status.caption("Thinking...")
            for event in agent_events(prompt, user, st.session_state.session_id):
                kind = event.get("type")
                if kind == "text":
                    status.empty()
                    yield event["text"]
                elif kind == "tool":
                    status.caption(f"Using {event['name'].split('___')[-1].replace('_', ' ')}...")
                elif kind == "done":
                    done.update(event)
                elif kind == "error":
                    raise RuntimeError(event["error"])

        tools = ""
        try:
            answer = st.write_stream(answer_chunks())
            status.empty()
            tools = ", ".join(n.split("___")[-1] for n in done.get("tools_used") or {})
        except Exception as e:
            log.exception("Agent call failed")
            status.empty()
            answer = f"Sorry, something went wrong calling the agent: {e}"

    st.session_state.messages.append({"role": "assistant", "content": str(answer), "tools": tools})
    list_chats.clear()      # refresh the sidebar - a new chat now has a title
    load_chat.clear()
    st.rerun()
