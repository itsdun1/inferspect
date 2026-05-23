// Shared domain types used by BOTH frontends (web-chat + web-insights).
//
// Per the plan: TS types are shared; API clients and auth helpers are NOT
// (each surface points at a different backend with a different cookie). Keep
// this file to pure interfaces — no fetch helpers, no env reads, no React.

// ── Auth ──────────────────────────────────────────────────────────────
// The same user-shape happens to come back from both chat-service's
// `/auth/users/me` (chat user) and insights-api's `/auth/users/me` (operator)
// because both run fastapi-users. The `role` field is chat-side semantics
// only; operator records ignore it.
export interface AuthUser {
  id: string;
  email: string;
  role: "user" | "admin";
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  created_at: string;
}

// ── Chat domain ───────────────────────────────────────────────────────
export type MessageRole = "user" | "assistant" | "system" | "tool";
export type MessageStatus = "complete" | "streaming" | "cancelled" | "error";

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

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
}

export interface SendMessageResponse {
  conversation_id: string;
  user_message: Message;
  assistant_message: Message;
  inference_request_id?: string | null;
}

// ── Insights / log shapes (cross both UIs because admin views render
//    a flavor of the same session timeline data) ───────────────────────
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

// ── Admin shapes used by web-insights ─────────────────────────────────
export interface SyntheticResult {
  inference_events: number;
  tool_events: number;
  error_events: number;
}
