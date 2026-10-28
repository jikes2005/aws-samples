import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface UiMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  tools?: string[];
  status?: string; // "Thinking..." / "Using list customers..." while streaming
  error?: boolean;
  streaming?: boolean;
}

const pretty = (tool: string) => tool.replace(/_/g, " ");

export default function Message({ m }: { m: UiMessage }) {
  if (m.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="bubble">{m.content}</div>
      </div>
    );
  }
  return (
    <div className={`msg msg-assistant ${m.error ? "msg-error" : ""}`}>
      <div className="avatar avatar-bot" aria-hidden="true" />
      <div className="msg-body">
        {m.content ? (
          <div className="markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
              }}
            >
              {m.content}
            </ReactMarkdown>
            {m.streaming && <span className="caret" aria-hidden="true" />}
          </div>
        ) : null}
        {m.status && (
          <div className="status" role="status">
            <span className="dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            {m.status}
          </div>
        )}
        {!m.streaming && m.tools && m.tools.length > 0 && (
          <div className="tools">
            {m.tools.map((t) => (
              <span key={t} className="chip">
                {pretty(t)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

