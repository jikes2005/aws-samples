import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type ChatSummary,
  getChat,
  listChats,
  loadConfig,
  newChatId,
  streamMessage,
} from "./api";
import Composer from "./components/Composer";
import Message, { type UiMessage } from "./components/Message";
import NameGate from "./components/NameGate";
import Sidebar from "./components/Sidebar";
import { savedUser, saveUser } from "./user";

const SUGGESTIONS = [
  "List active customers in Ontario",
  "What accounts does C-1002 have, and what's the total balance?",
  "Onboard Dana Lee, dana.lee@example.com, Ottawa ON, retail",
];

let seq = 0;
const uid = () => `m${Date.now()}-${seq++}`;
const pretty = (tool: string) => tool.replace(/_/g, " ");

export default function App() {
  const [ready, setReady] = useState(false);
  const [apiBase, setApiBase] = useState("");
  const [user, setUser] = useState(savedUser);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [chatId, setChatId] = useState(newChatId);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadConfig().then((base) => {
      setApiBase(base);
      setReady(true);
    });
  }, []);

  const refreshChats = useCallback(async () => {
    if (!user) return;
    setChatsLoading(true);
    try {
      setChats((await listChats(user)).chats);
    } catch (e) {
      setBanner(e instanceof ApiError ? e.message : String(e));
    } finally {
      setChatsLoading(false);
    }
  }, [user]);

  useEffect(() => {
    if (ready && user) refreshChats();
  }, [ready, user, refreshChats]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const patchLast = (fn: (m: UiMessage) => UiMessage) =>
    setMessages((ms) => (ms.length ? [...ms.slice(0, -1), fn(ms[ms.length - 1])] : ms));

  const send = async (prompt: string) => {
    if (busy) return;
    setBanner("");
    setBusy(true);
    setMessages((ms) => [
      ...ms,
      { id: uid(), role: "user", content: prompt },
      { id: uid(), role: "assistant", content: "", status: "Thinking...", streaming: true },
    ]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamMessage(
        user,
        chatId,
        prompt,
        (e) => {
          if (e.type === "text") patchLast((m) => ({ ...m, content: m.content + e.text, status: undefined }));
          else if (e.type === "tool") patchLast((m) => ({ ...m, status: `Using ${pretty(e.name)}...` }));
          else if (e.type === "done") patchLast((m) => ({ ...m, tools: e.toolsUsed }));
          else if (e.type === "error")
            patchLast((m) => ({
              ...m,
              error: true,
              content: `${m.content ? m.content + "\n\n" : ""}Sorry, something went wrong calling the agent: ${e.error}`,
            }));
        },
        ctrl.signal,
      );
      if (ctrl.signal.aborted) patchLast((m) => ({ ...m, content: m.content || "_Stopped._" }));
    } catch (e) {
      patchLast((m) => ({ ...m, error: true, content: e instanceof Error ? e.message : String(e) }));
    } finally {
      patchLast((m) => ({ ...m, streaming: false, status: undefined }));
      abortRef.current = null;
      setBusy(false);
      refreshChats(); // a new chat now has a title
    }
  };

  const openChat = async (id: string) => {
    setMenuOpen(false);
    if (id === chatId) return;
    setBanner("");
    setChatId(id);
    setMessages([]);
    try {
      const chat = await getChat(user, id);
      setMessages(chat.messages.map((m) => ({ id: uid(), role: m.role, content: m.content })));
    } catch (e) {
      setBanner(e instanceof ApiError ? e.message : String(e));
    }
  };

  const newChat = () => {
    setMenuOpen(false);
    setBanner("");
    setChatId(newChatId());
    setMessages([]);
  };

  const changeName = () => {
    saveUser("");
    setUser("");
    setChats([]);
    newChat();
  };

  if (!ready) return <div className="splash">Loading...</div>;
  if (!user)
    return (
      <NameGate
        onSubmit={(name) => {
          saveUser(name);
          setUser(name);
        }}
      />
    );

  return (
    <div className="layout">
      <Sidebar
        user={user}
        chats={chats}
        activeId={chatId}
        loading={chatsLoading}
        busy={busy}
        open={menuOpen}
        onNewChat={newChat}
        onSelect={openChat}
        onChangeName={changeName}
        onClose={() => setMenuOpen(false)}
      />
      {menuOpen && <div className="scrim only-mobile" onClick={() => setMenuOpen(false)} />}

      <main className="main">
        <header className="topbar">
          <button className="icon-btn only-mobile" onClick={() => setMenuOpen(true)} aria-label="Open menu">
            ☰
          </button>
          <h1>{chats.find((c) => c.id === chatId)?.title ?? "New chat"}</h1>
        </header>

        {!apiBase && (
          <div className="banner">
            The API address isn't configured (API_BASE_URL). Calls go to this page's own server.
          </div>
        )}
        {banner && (
          <div className="banner" role="alert">
            {banner}
            <button className="link-btn" onClick={() => setBanner("")}>
              Dismiss
            </button>
          </div>
        )}

        <section className="thread" aria-live="polite">
          {messages.length === 0 ? (
            <div className="empty">
              <h2>How can I help?</h2>
              <p className="muted">
                Look up a customer, list them by province or segment, or onboard a new one. I remember
                your preferences between chats.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="suggestion" onClick={() => send(s)} disabled={busy}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m) => <Message key={m.id} m={m} />)
          )}
          <div ref={endRef} />
        </section>

        <div className="composer-wrap">
          <Composer busy={busy} onSend={send} onStop={() => abortRef.current?.abort()} />
          <div className="hint muted small">Enter to send · Shift+Enter for a new line · Demo data only</div>
        </div>
      </main>
    </div>
  );
}
