import { useState } from "react";

export default function NameGate({ onSubmit }: { onSubmit: (name: string) => void }) {
  const [name, setName] = useState("");
  const valid = /[a-z0-9]/i.test(name);

  return (
    <div className="gate">
      <form
        className="gate-card"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) onSubmit(name.trim());
        }}
      >
        <div className="brand brand-lg">
          <span className="brand-mark" aria-hidden="true" />
          Customer Assistant
        </div>
        <p className="muted">
          Lab version - no sign-in. Your name keeps your chats, and the assistant's memory of you,
          separate from other people's.
        </p>
        <label htmlFor="name">Your name</label>
        <input
          id="name"
          autoFocus
          autoComplete="off"
          placeholder="e.g. jane.doe"
          value={name}
          maxLength={64}
          onChange={(e) => setName(e.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={!valid}>
          Start
        </button>
      </form>
    </div>
  );
}
