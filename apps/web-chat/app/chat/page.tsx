"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  chatApi,
  logout,
  me,
  streamChat,
  type ChatStreamEvent,
} from "../../lib/api";
import type {
  AuthUser,
  Conversation,
  ConversationDetail,
  ConversationListItem,
  Message,
} from "@ollive/web-shared";

// Tool calls captured during a streaming turn are attached to the assistant
// message via this side-channel field. The bubble renders them inline.
type ToolEvent = { name: string; args: string; result?: string };
type MessageWithTools = Message & { tools?: ToolEvent[] };

// Models the chat-service exposes. Keep this in sync with langchain_adapter.py.
// Provider is routed via name prefix (gpt*/o1* → OpenAI, claude* → Anthropic, gemini* → Google).
const MODELS = [
  { value: "gpt-4o", label: "GPT-4o" },
  { value: "gpt-4o-mini", label: "GPT-4o Mini" },
  { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5" },
  { value: "claude-sonnet-4-5-20250929", label: "Claude Sonnet 4.5" },
  { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
  { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
] as const;

const DEFAULT_MODEL = MODELS[0].value;

const SUGGESTIONS = [
  "What does Ollive cover?",
  "Get a quote for a $5M SaaS using gen-AI",
  "Does Ollive cover hallucinations?",
  "How do I file a claim?",
  "What's NOT covered?",
] as const;

function relativeTime(iso: string): string {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (Number.isNaN(diff)) return "";
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function StatusBadge({ status }: { status: Message["status"] }) {
  if (status === "cancelled") {
    return (
      <span className="ml-2 inline-flex items-center rounded-full bg-gray-200 px-2 py-0.5 text-xs font-medium text-gray-600">
        (cancelled)
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="ml-2 inline-flex items-center rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
        (error)
      </span>
    );
  }
  return null;
}

export default function ChatPage() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [model, setModel] = useState<string>(DEFAULT_MODEL);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [backendError, setBackendError] = useState<string | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Hydrate auth state on mount. The proxy gates this page server-side, but
  // we still fetch the user record so we can show the email.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await me();
        if (!cancelled) setUser(u);
      } catch {
        if (!cancelled) router.replace("/login");
      }
    })();
    return () => { cancelled = true; };
  }, [router]);

  const refreshConversations = useCallback(async () => {
    try {
      const list = await chatApi.get<ConversationListItem[]>(
        "/conversations?limit=50&offset=0",
      );
      setConversations(list);
      setBackendError(null);
    } catch (err) {
      const msg = err instanceof ApiError
        ? `Backend error (${err.status})`
        : "Backend not available";
      setBackendError(msg);
      setConversations([]);
    }
  }, []);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  // Load active conversation transcript.
  useEffect(() => {
    if (!activeId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    (async () => {
      setLoadingDetail(true);
      try {
        const detail = await chatApi.get<ConversationDetail>(
          `/conversations/${activeId}`,
        );
        if (cancelled) return;
        setMessages(detail.messages);
        if (detail.conversation.model) setModel(detail.conversation.model);
        setBackendError(null);
      } catch (err) {
        if (cancelled) return;
        const msg = err instanceof ApiError
          ? `Failed to load conversation (${err.status})`
          : "Backend not available";
        setBackendError(msg);
        setMessages([]);
      } finally {
        if (!cancelled) setLoadingDetail(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  // Auto-scroll transcript to bottom on new messages.
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, loadingDetail]);

  async function handleNewChat() {
    try {
      const convo = await chatApi.post<Conversation>("/conversations", {
        model,
      });
      setActiveId(convo.id);
      setMessages([]);
      await refreshConversations();
    } catch (err) {
      const msg = err instanceof ApiError
        ? `Could not create conversation (${err.status})`
        : "Backend not available";
      setBackendError(msg);
    }
  }

  async function handleSend(e: React.FormEvent, override?: string) {
    e.preventDefault();
    const text = (override ?? input).trim();
    if (!text || sending) return;
    setSending(true);

    const userTempId = `tmp-user-${Date.now()}`;
    const assistTempId = `tmp-assist-${Date.now()}`;
    const optimisticUser: Message = {
      id: userTempId,
      conversation_id: activeId ?? "",
      role: "user",
      content: text,
      status: "complete",
      created_at: new Date().toISOString(),
    };
    const streamingAssistant: MessageWithTools = {
      id: assistTempId,
      conversation_id: activeId ?? "",
      role: "assistant",
      content: "",
      status: "streaming",
      created_at: new Date().toISOString(),
      tools: [],
    };
    setMessages((prev) => [...prev, optimisticUser, streamingAssistant]);
    setInput("");

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      for await (const ev of streamChat(
        { conversation_id: activeId, content: text, model },
        ctrl.signal,
      )) {
        applyStreamEvent(ev, assistTempId, setActiveId, setMessages);
      }
      void refreshConversations();
      setBackendError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        // Cancellation handled by the cancelled event from the server.
      } else {
        const msg = err instanceof ApiError
          ? `Send failed (${err.status})`
          : "Backend not available";
        setBackendError(msg);
        setMessages((prev) => prev.filter((m) => m.id !== userTempId && m.id !== assistTempId));
        setInput(text);
      }
    } finally {
      abortRef.current = null;
      setSending(false);
    }
  }

  async function handleCancel() {
    if (activeId) {
      try {
        await chatApi.post<Conversation>(`/conversations/${activeId}/cancel`);
      } catch {
        // Best-effort — even if the cancel HTTP fails, aborting the stream
        // stops the UI from spinning.
      }
    }
    abortRef.current?.abort();
    setSending(false);
    void refreshConversations();
  }

  async function handleSignOut() {
    try {
      await logout();
    } catch {
      // best-effort; the cookie expires server-side eventually
    }
    setUser(null);
    setConversations([]);
    setMessages([]);
    setActiveId(null);
    router.replace("/login");
  }

  const sidebarItems = useMemo(
    () =>
      conversations.map((c) => {
        const isActive = c.id === activeId;
        return (
          <button
            key={c.id}
            onClick={() => setActiveId(c.id)}
            className={`w-full text-left rounded-md px-3 py-2 text-sm transition border ${
              isActive
                ? "border-blue-200 bg-blue-50 text-blue-900"
                : "border-transparent hover:bg-gray-100 text-gray-800"
            }`}
          >
            <div className="font-medium truncate">
              {c.title || "Untitled conversation"}
            </div>
            <div className="mt-0.5 flex items-center justify-between text-xs text-gray-500">
              <span className="truncate">{c.model ?? "—"}</span>
              <span className="ml-2 shrink-0">{relativeTime(c.updated_at)}</span>
            </div>
          </button>
        );
      }),
    [conversations, activeId],
  );

  return (
    <div className="flex flex-1 min-h-0 h-[100dvh]">
      {/* Sidebar */}
      <aside className="w-72 shrink-0 border-r border-gray-200 bg-white flex flex-col">
        <div className="px-4 py-3 border-b border-gray-200 flex items-center justify-between">
          <span className="text-sm font-semibold text-gray-900">Ovel</span>
        </div>
        <div className="p-3">
          <button
            onClick={handleNewChat}
            className="w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-blue-700"
          >
            + New chat
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-1">
          {conversations.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-gray-500">
              No conversations yet.
            </div>
          ) : (
            sidebarItems
          )}
        </div>
        <div className="border-t border-gray-200 px-4 py-3 text-xs text-gray-600">
          {user ? (
            <div className="flex items-center justify-between gap-2">
              <span className="truncate" title={user.email}>{user.email}</span>
              <button
                onClick={() => void handleSignOut()}
                className="text-blue-600 hover:text-blue-800 transition"
              >
                Sign out
              </button>
            </div>
          ) : (
            <Link
              href="/login"
              className="text-blue-600 hover:text-blue-800 transition"
            >
              Sign in →
            </Link>
          )}
        </div>
      </aside>

      {/* Main pane */}
      <section className="flex-1 min-w-0 flex flex-col bg-gray-50">
        <header className="border-b border-gray-200 bg-white px-6 py-3 flex items-center justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-base font-semibold text-gray-900 truncate">
              {activeId
                ? conversations.find((c) => c.id === activeId)?.title ||
                  "Untitled conversation"
                : "New chat"}
            </h1>
            {backendError ? (
              <p className="mt-0.5 text-xs text-red-600">{backendError}</p>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <label htmlFor="model" className="text-xs text-gray-500">
              Model
            </label>
            <select
              id="model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="rounded-md border border-gray-300 bg-white px-2 py-1 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              {MODELS.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>
        </header>

        <div
          ref={transcriptRef}
          className="flex-1 min-h-0 overflow-y-auto px-6 py-4 space-y-4"
        >
          {loadingDetail ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : messages.length === 0 ? (
            <div className="mx-auto max-w-md text-center text-sm text-gray-500 mt-16">
              {backendError ? (
                <>
                  <p className="font-medium text-gray-700">
                    Backend not available
                  </p>
                  <p className="mt-1">
                    Make sure the chat service is running on{" "}
                    <code className="rounded bg-gray-100 px-1 py-0.5 text-xs">
                      http://localhost:8000
                    </code>
                    .
                  </p>
                </>
              ) : (
                <div>
                  <p className="text-lg font-medium text-gray-800">
                    Hi, I&apos;m Ollie 👋
                  </p>
                  <p className="mt-1 text-sm text-gray-500">
                    Ask me anything about Ollive&apos;s AI liability coverage,
                    pricing, or claims. Or try one of these:
                  </p>
                  <div className="mt-5 flex flex-wrap justify-center gap-2">
                    {SUGGESTIONS.map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={(e) => {
                          void handleSend(e as unknown as React.FormEvent, s);
                        }}
                        className="rounded-full border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-700 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700 transition"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            messages.map((m) => (
              <MessageBubble key={m.id} message={m} />
            ))
          )}
        </div>

        <form
          onSubmit={handleSend}
          className="border-t border-gray-200 bg-white px-6 py-3"
        >
          <div className="flex gap-2 items-end">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void handleSend(e);
                }
              }}
              placeholder="Send a message…"
              rows={2}
              className="flex-1 resize-none rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            {sending ? (
              <button
                type="button"
                onClick={handleCancel}
                className="rounded-md bg-gray-200 px-4 py-2 text-sm font-medium text-gray-800 transition hover:bg-gray-300"
              >
                Cancel
              </button>
            ) : (
              <button
                type="submit"
                disabled={!input.trim()}
                className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:bg-blue-300"
              >
                Send
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}

function MessageBubble({ message }: { message: MessageWithTools }) {
  const isUser = message.role === "user";
  const isStreaming = message.status === "streaming";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] rounded-lg px-4 py-2 text-sm shadow-sm ${
          isUser
            ? "bg-blue-600 text-white whitespace-pre-wrap"
            : "bg-white text-gray-900 border border-gray-200"
        }`}
      >
        <div className="text-xs font-medium opacity-70 mb-1 flex items-center">
          <span>{isUser ? "You" : message.role}</span>
          <StatusBadge status={message.status} />
        </div>
        {message.tools?.length ? (
          <div className="mb-2 space-y-1">
            {message.tools.map((t, i) => (
              <details
                key={i}
                className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-900"
              >
                <summary className="cursor-pointer font-medium select-none">
                  🛠 {t.name}
                  {t.result ? null : (
                    <span className="ml-2 font-mono text-[10px] text-amber-600">
                      running…
                    </span>
                  )}
                </summary>
                <div className="mt-1 space-y-1">
                  <div className="font-mono text-[10px] text-amber-700 break-all">
                    args: {t.args}
                  </div>
                  {t.result ? (
                    <div className="font-mono text-[10px] text-amber-700 whitespace-pre-wrap break-words max-h-32 overflow-y-auto">
                      → {t.result}
                    </div>
                  ) : null}
                </div>
              </details>
            ))}
          </div>
        ) : null}
        {isUser ? (
          <div>{message.content}</div>
        ) : (
          <div className="markdown-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content || (message.status === "cancelled" ? "_(cancelled)_" : "")}
            </ReactMarkdown>
            {isStreaming ? (
              <span className="ml-0.5 inline-block animate-pulse">▍</span>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Apply one streamed SSE event to the message list. Mutates the in-flight
 * assistant message (identified by `assistTempId`) in place.
 */
function applyStreamEvent(
  ev: ChatStreamEvent,
  assistTempId: string,
  setActiveId: React.Dispatch<React.SetStateAction<string | null>>,
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>,
) {
  switch (ev.kind) {
    case "start":
      setActiveId(ev.conversation_id);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistTempId
            ? { ...m, conversation_id: ev.conversation_id }
            : m,
        ),
      );
      break;
    case "delta":
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistTempId
            ? { ...m, content: m.content + ev.content }
            : m,
        ),
      );
      break;
    case "tool_call":
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== assistTempId) return m;
          const wm = m as MessageWithTools;
          return { ...wm, tools: [...(wm.tools ?? []), { name: ev.name, args: ev.args }] };
        }),
      );
      break;
    case "tool_result":
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== assistTempId) return m;
          const wm = m as MessageWithTools;
          const tools = (wm.tools ?? []).slice();
          // Attach the result to the most recent tool entry of this name without a result yet.
          for (let i = tools.length - 1; i >= 0; i--) {
            if (tools[i].name === ev.name && tools[i].result === undefined) {
              tools[i] = { ...tools[i], result: ev.result };
              break;
            }
          }
          return { ...wm, tools };
        }),
      );
      break;
    case "done":
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistTempId
            ? { ...m, id: ev.assistant_message_id, status: "complete", inference_request_id: ev.inference_request_id }
            : m,
        ),
      );
      break;
    case "cancelled":
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistTempId
            ? { ...m, id: ev.assistant_message_id, status: "cancelled", content: ev.partial_content || m.content }
            : m,
        ),
      );
      break;
  }
}
