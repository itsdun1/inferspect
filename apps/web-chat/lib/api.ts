// Thin fetch wrapper for the chat-service. Auth is cookie-based
// (fastapi-users JWT in `chat_session`). Every request sends
// ``credentials: "include"`` so the browser ships the cookie automatically.
//
// This client is INTENTIONALLY scoped to chat-service — the operator console
// lives in apps/web-insights and hits insights-api via a separate client.

export const CHAT_API_URL =
  process.env.NEXT_PUBLIC_CHAT_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  baseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers,
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // ignore — non-JSON error bodies are fine
    }
    throw new ApiError(res.status, body, `HTTP ${res.status} on ${path}`);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? (JSON.parse(text) as T) : (undefined as T));
}

export const chatApi = {
  get: <T>(path: string, init?: RequestInit) =>
    request<T>(CHAT_API_URL, path, { ...init, method: "GET" }),
  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    request<T>(CHAT_API_URL, path, {
      ...init,
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
};

// -- Streaming chat (SSE) -----------------------------------------------------

export type ChatStreamEvent =
  | { kind: "start"; conversation_id: string; user_message_id: string; model: string }
  | { kind: "delta"; content: string }
  | { kind: "tool_call"; name: string; args: string }
  | { kind: "tool_result"; name: string; result: string }
  | { kind: "done"; assistant_message_id: string; inference_request_id: string | null }
  | { kind: "cancelled"; assistant_message_id: string; partial_content: string };

/**
 * Open POST /chat/stream and async-yield each parsed SSE event.
 * The signal lets the caller abort the connection (used for cancel).
 */
export async function* streamChat(
  body: { conversation_id: string | null; content: string; model?: string },
  signal?: AbortSignal,
): AsyncIterable<ChatStreamEvent> {
  const res = await fetch(`${CHAT_API_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
    cache: "no-store",
    signal,
  });
  if (!res.ok || !res.body) {
    let detail: unknown = null;
    try { detail = await res.json(); } catch {}
    throw new ApiError(res.status, detail, `stream open failed (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE frames are separated by blank lines.
    let blank: number;
    while ((blank = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, blank);
      buf = buf.slice(blank + 2);
      const ev = parseSseFrame(frame);
      if (ev) yield ev;
    }
  }
}

function parseSseFrame(frame: string): ChatStreamEvent | null {
  let event = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!event || !data) return null;
  try {
    return { kind: event as ChatStreamEvent["kind"], ...JSON.parse(data) } as ChatStreamEvent;
  } catch {
    return null;
  }
}

// -- Auth helpers --------------------------------------------------------------
// All hit chat-service. The cookie set by these is `chat_session`.

import type { AuthUser } from "@ollive/web-shared";

export async function login(email: string, password: string): Promise<void> {
  // fastapi-users login expects application/x-www-form-urlencoded with
  // fields ``username`` and ``password`` (OAuth2 password flow shape).
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${CHAT_API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
    credentials: "include",
  });
  if (!res.ok) {
    let detail: unknown = null;
    try {
      detail = await res.json();
    } catch {}
    throw new ApiError(res.status, detail, `login failed (${res.status})`);
  }
}

export async function register(
  email: string,
  password: string,
): Promise<AuthUser> {
  return chatApi.post<AuthUser>("/auth/register", { email, password });
}

export async function logout(): Promise<void> {
  await chatApi.post<void>("/auth/logout");
}

export async function me(): Promise<AuthUser> {
  return chatApi.get<AuthUser>("/auth/users/me");
}
