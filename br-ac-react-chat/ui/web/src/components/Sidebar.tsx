import type { ChatSummary } from "../api";

interface Props {
  user: string;
  chats: ChatSummary[];
  activeId: string;
  loading: boolean;
  busy: boolean;
  open: boolean;
  onNewChat: () => void;
  onSelect: (id: string) => void;
  onChangeName: () => void;
  onClose: () => void;
}

function when(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days < 1) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function Sidebar(p: Props) {
  return (
    <aside className={`sidebar ${p.open ? "open" : ""}`} aria-label="Chats">
      <div className="sidebar-top">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          Customer Assistant
        </div>
        <button className="icon-btn only-mobile" onClick={p.onClose} aria-label="Close menu">
          ✕
        </button>
      </div>

      <button className="btn btn-primary btn-block" onClick={p.onNewChat} disabled={p.busy}>
        + New chat
      </button>

      <div className="section-label">Recent chats</div>
      <nav className="chat-list">
        {p.loading && p.chats.length === 0 && <div className="muted small pad">Loading...</div>}
        {!p.loading && p.chats.length === 0 && <div className="muted small pad">No chats yet</div>}
        {p.chats.map((c) => (
          <button
            key={c.id}
            className={`chat-item ${c.id === p.activeId ? "active" : ""}`}
            onClick={() => p.onSelect(c.id)}
            disabled={p.busy}
            title={c.title}
          >
            <span className="chat-title">{c.title}</span>
            <span className="chat-when">{when(c.createdAt)}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div className="avatar" aria-hidden="true">
          {p.user.slice(0, 1).toUpperCase()}
        </div>
        <div className="who" title={p.user}>
          {p.user}
        </div>
        <button className="link-btn" onClick={p.onChangeName} disabled={p.busy}>
          Change
        </button>
      </div>
    </aside>
  );
}
