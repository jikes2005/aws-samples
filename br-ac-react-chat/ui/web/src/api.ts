// Calls to the br-ac-react-chat REST API (API Gateway -> ECS). The browser sends the user's
// name in X-Chat-User (lab version - no sign-in).

export type Role = "user" | "assistant";

export interface ChatSummary {
  id: string;
  title: string;
  createdAt?: string;
}

export interface ChatMessage {
  role: Role;
  content: string;
}

export type StreamEvent =
  | { type: "text"; text: string }
  | { type: "tool"; name: string }
  | { type: "done"; toolsUsed: string[] }
  | { type: "error"; error: string };

let apiBaseUrl = "";

/** Reads /config.json (written by the UI server from the stack's settings). */
export async function loadConfig(): Promise<string> {
  const fromEnv = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
  try {
    const res = await fetch("./config.json", { cache: "no-store" });
    if (res.ok) {
      const cfg = (await res.json()) as { apiBaseUrl?: string };
      apiBaseUrl = (cfg.apiBaseUrl || fromEnv).replace(/\/+$/, "");
      return apiBaseUrl;
    }
  } catch {
    /* fall through to the build-time setting */
  }
  apiBaseUrl = fromEnv.replace(/\/+$/, "");
  return apiBaseUrl;
}

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
  }
}

function headers(user: string, json = false): HeadersInit {
  const h: Record<string, string> = { "X-Chat-User": user };
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function request<T>(path: string, user: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl}${path}`, { headers: headers(user) });
  } catch {
    throw new ApiError(
      "Can't reach the API. Check that this browser can resolve and reach the API Gateway address " +
        "(the execute-api VPC endpoint), and that the API's CORS settings allow this page.",
    );
  }
  if (!res.ok) throw new ApiError(await errorText(res), res.status);
  return (await res.json()) as T;
}

async function errorText(res: Response): Promise<string> {
  let detail = "";
  try {
    const body = await res.json();
    detail = body.detail ?? body.message ?? "";
  } catch {
    /* not JSON */
  }
  if (res.status === 403)
    return `The API refused the request (403${detail ? `: ${detail}` : ""}). A WAF or the API's resource policy may be blocking this network.`;
  return `API error ${res.status}${detail ? `: ${detail}` : ""}`;
}

export const listChats = (user: string) =>
  request<{ user: string; chats: ChatSummary[] }>("/api/chats", user);

export const getChat = (user: string, id: string) =>
  request<{ id: string; messages: ChatMessage[] }>(`/api/chats/${encodeURIComponent(id)}`, user);

/** Sends a message and calls onEvent for each server-sent event as it arrives. */
export async function streamMessage(
  user: string,
  chatId: string,
  prompt: string,
  onEvent: (e: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl}/api/chats/${encodeURIComponent(chatId)}/messages`, {
      method: "POST",
      headers: headers(user, true),
      body: JSON.stringify({ prompt }),
      signal,
    });
  } catch (e) {
    if (signal.aborted) return;
    throw new ApiError("Can't reach the API to send your message.");
  }
  if (!res.ok || !res.body) throw new ApiError(await errorText(res), res.status);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Server-sent events are separated by a blank line; each has "data: {json}" lines.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data:")) {
          try {
            onEvent(JSON.parse(line.slice(5).trim()) as StreamEvent);
          } catch {
            /* ignore a malformed line */
          }
        }
      }
    }
  }
}

export function newChatId(): string {
  const uuid =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
          const r = (Math.random() * 16) | 0;
          return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
        });
  return `chat-${uuid}`; // runtime session IDs must be at least 33 characters
}
