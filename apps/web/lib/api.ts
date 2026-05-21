// Thin fetch wrapper for the chat-service + insights-api.
// Auth is cookie-based (fastapi-users JWT in an HttpOnly cookie). Every fetch
// sends ``credentials: "include"`` so the browser ships the cookie automatically.

export const CHAT_API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const INSIGHTS_API_URL =
  process.env.NEXT_PUBLIC_INSIGHTS_URL ?? "http://localhost:8003";

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

export const insightsApi = {
  get: <T>(path: string, init?: RequestInit) =>
    request<T>(INSIGHTS_API_URL, path, { ...init, method: "GET" }),
};

// -- Auth helpers --------------------------------------------------------------

export interface AuthUser {
  id: string;
  email: string;
  role: "user" | "admin";
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  created_at: string;
}

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

// -- Admin helpers -------------------------------------------------------------

export async function adminListUsers(): Promise<AuthUser[]> {
  return chatApi.get<AuthUser[]>("/admin/users");
}

export interface SyntheticResult {
  inference_events: number;
  tool_events: number;
  error_events: number;
}

export async function adminGenerateSynthetic(
  body: {
    count?: number;
    error_rate?: number;
    tool_call_rate?: number;
    spread_seconds?: number;
  } = {},
): Promise<SyntheticResult> {
  return chatApi.post<SyntheticResult>("/admin/synthetic", { count: 100, ...body });
}

export async function adminListAllConversations(): Promise<ConversationListItem[]> {
  return chatApi.get<ConversationListItem[]>("/admin/conversations");
}

// -- Domain types --------------------------------------------------------------

export type MessageRole = "user" | "assistant" | "system" | "tool";

export type MessageStatus =
  | "complete"
  | "streaming"
  | "cancelled"
  | "error";

export interface Message {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  status: MessageStatus;
  created_at: string;
  inference_request_id?: string | null;
}

export interface Conversation {
  id: string;
  user_id: string;
  title: string | null;
  status: "active" | "cancelled" | "completed";
  model: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConversationListItem {
  id: string;
  title: string | null;
  status: "active" | "cancelled" | "completed";
  model: string | null;
  message_count: number;
  updated_at: string;
}

export interface SendMessageResponse {
  conversation_id: string;
  user_message: Message;
  assistant_message: Message;
  inference_request_id?: string | null;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
}

export interface LatencyPoint {
  bucket: string;
  provider?: string;
  model?: string;
  p50: number;
  p95: number;
  p99: number;
}

export interface ThroughputPoint {
  bucket: string;
  provider?: string;
  requests_per_min: number;
  tokens_per_min: number;
}

export interface CostPoint {
  model: string;
  provider?: string;
  cost_usd: number;
}

export interface TopConversation {
  conversation_id: string;
  session_id?: string;
  provider?: string;
  model?: string;
  cost_usd?: number;
  tokens?: number;
  avg_latency_ms?: number;
  max_latency_ms?: number;
  req_count: number;
  metric_value: number;
}

export interface SessionTimelineEntry {
  ts?: string;
  started_at?: string;
  finished_at?: string;
  event_type: "inference" | "application" | "tool_execution";
  kind?: "inference" | "application" | "tool_execution";
  service?: string | null;
  level?: string | null;
  model?: string | null;
  provider?: string | null;
  message?: string | null;
  latency_ms?: number | null;
  status?: string | null;
  cost_usd?: number | null;
  total_tokens?: number | null;
  input_preview?: string | null;
  output_preview?: string | null;
}

export interface SessionTimeline {
  session_id: string;
  inference_count?: number;
  tool_count?: number;
  timeline: SessionTimelineEntry[];
}

export interface ErrorsGroup {
  error_code: string;
  provider: string;
  error_count: number;
  samples: string[];
}

export interface ToolStat {
  tool_name: string;
  call_count: number;
  error_count: number;
  error_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  total_bytes: number;
}
