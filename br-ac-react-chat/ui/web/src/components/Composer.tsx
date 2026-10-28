import { useEffect, useRef, useState } from "react";

interface Props {
  busy: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

export default function Composer({ busy, onSend, onStop }: Props) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // Grow with the text, up to a limit
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  useEffect(() => {
    if (!busy) ref.current?.focus();
  }, [busy]);

  const send = () => {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t);
    setText("");
  };

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        ref={ref}
        rows={1}
        value={text}
        maxLength={8000}
        placeholder="Ask about a customer..."
        aria-label="Message"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      {busy ? (
        <button type="button" className="btn btn-secondary" onClick={onStop}>
          Stop
        </button>
      ) : (
        <button type="submit" className="btn btn-primary" disabled={!text.trim()}>
          Send
        </button>
      )}
    </form>
  );
}
