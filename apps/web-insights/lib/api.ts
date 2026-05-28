// Thin fetch wrapper for the insights-api (operator console backend).
// Auth is cookie-based (fastapi-users JWT in `console_session`). Every
// request sends ``credentials: "include"`` so the cookie is shipped.
//
// IMPORTANT: this client is scoped to insights-api ONLY. The chat surface
// lives in apps/web-chat and hits chat-service via its own client. The two
// surfaces have INDEPENDENT sessions — cookies don't collide because they
// live on different subdomains in prod and different ports locally.

export const INSIGHTS_API_URL =
  process.env.NEXT_PUBLIC_INSIGHTS_API_URL ?? "http://localhost:8003";

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
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${INSIGHTS_API_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {}
    throw new ApiError(res.status, body, `HTTP ${res.status} on ${path}`);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? (JSON.parse(text) as T) : (undefined as T));
}

export const insightsApi = {
  get: <T>(path: string, init?: RequestInit) =>
    request<T>(path, { ...init, method: "GET" }),
  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    request<T>(path, {
      ...init,
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
};

// -- Auth helpers (operators) -------------------------------------------------
// insights-api mounts the fastapi-users auth routers at /auth (see
// apps/insights-api/insights_api/main.py). Login is OAuth2 password flow.

import type { AuthUser, ConversationListItem, SyntheticResult } from "@ollive/web-shared";

export async function login(email: string, password: string): Promise<void> {
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${INSIGHTS_API_URL}/auth/login`, {
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

export async function logout(): Promise<void> {
  await insightsApi.post<void>("/auth/logout");
}

export async function me(): Promise<AuthUser> {
  return insightsApi.get<AuthUser>("/auth/users/me");
}

// -- Admin helpers (operator-only, gated by current_active_operator) ----------

export async function adminListUsers(): Promise<AuthUser[]> {
  return insightsApi.get<AuthUser[]>("/admin/users");
}

export async function adminListAllConversations(): Promise<ConversationListItem[]> {
  return insightsApi.get<ConversationListItem[]>("/admin/conversations");
}

export async function adminGenerateSynthetic(
  body: {
    count?: number;
    error_rate?: number;
    tool_call_rate?: number;
    spread_seconds?: number;
  } = {},
): Promise<SyntheticResult> {
  return insightsApi.post<SyntheticResult>("/admin/synthetic", { count: 100, ...body });
}

// -- Agent fleet (Phase G) ----------------------------------------------------

export interface AgentRow {
  host_id: string;
  last_seen: string | null;
  is_live: boolean;
  event_count: number;
  container_id: string | null;
  distinct_pids: number;
  distinct_providers: number;
}

export async function getAgents(): Promise<{ agents: AgentRow[] }> {
  return insightsApi.get<{ agents: AgentRow[] }>("/agents");
}

export interface KillResult {
  command_id: string;
  cursor: string;
  fingerprint: string;
  host_id: string;
}

export interface HostFingerprint {
  fingerprint: string;
  first_seen: string | null;
  last_seen: string | null;
  request_count: number;
  preview: string;
  model: string | null;
  provider: string | null;
  distinct_pids: number;
}

export async function getHostFingerprints(
  hostId: string,
  opts: { windowHours?: number; limit?: number } = {},
): Promise<{ fingerprints: HostFingerprint[] }> {
  const params = new URLSearchParams();
  if (opts.windowHours) params.set("window_hours", String(opts.windowHours));
  if (opts.limit) params.set("limit", String(opts.limit));
  const q = params.toString();
  return insightsApi.get<{ fingerprints: HostFingerprint[] }>(
    `/agents/${encodeURIComponent(hostId)}/fingerprints${q ? `?${q}` : ""}`,
  );
}

export async function killOnHost(
  hostId: string,
  body: { fingerprint: string; reason?: string; ttl_seconds?: number },
): Promise<KillResult> {
  return insightsApi.post<KillResult>(`/agents/${hostId}/kill`, body);
}

export async function killSession(body: {
  session_id: string;
  reason?: string;
  ttl_seconds?: number;
}): Promise<KillResult> {
  return insightsApi.post<KillResult>("/agents/kill-session", body);
}

// -- Enforcement events (audit log) -------------------------------------------

export interface EnforcementEvent {
  timestamp: string;
  host_id: string;
  fingerprint: string;
  command: string;
  reason: string;
  source: string;
  client: string;
  rule_id: string;
  operator_id: string;
  matched: number;
}

export async function getEnforcementEvents(
  opts: { hostId?: string; windowHours?: number; limit?: number } = {},
): Promise<{ events: EnforcementEvent[] }> {
  const params = new URLSearchParams();
  if (opts.hostId) params.set("host_id", opts.hostId);
  if (opts.windowHours) params.set("window_hours", String(opts.windowHours));
  if (opts.limit) params.set("limit", String(opts.limit));
  const q = params.toString();
  return insightsApi.get<{ events: EnforcementEvent[] }>(
    `/enforcement-events${q ? `?${q}` : ""}`,
  );
}
