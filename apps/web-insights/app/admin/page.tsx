"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ApiError,
  adminGenerateSynthetic,
  adminListAllConversations,
  adminListUsers,
  me,
} from "../../lib/api";
import type {
  AuthUser,
  ConversationListItem,
  SyntheticResult,
} from "@ollive/web-shared";

// Operator-only admin. The proxy gates entry on `console_session` cookie
// presence; this page additionally calls /auth/users/me to ensure the JWT
// still validates (and to surface the operator email in the header).
export default function AdminPage() {
  const router = useRouter();
  const [operator, setOperator] = useState<AuthUser | null>(null);
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [conversations, setConversations] = useState<ConversationListItem[] | null>(null);
  const [count, setCount] = useState(100);
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState<SyntheticResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await me();
        if (cancelled) return;
        setOperator(u);
        const [usersList, convosList] = await Promise.all([
          adminListUsers(),
          adminListAllConversations(),
        ]);
        if (!cancelled) {
          setUsers(usersList);
          setConversations(convosList);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
        } else {
          setError("Failed to load admin data.");
        }
      }
    })();
    return () => { cancelled = true; };
  }, [router]);

  async function handleGenerate() {
    setGenerating(true);
    setResult(null);
    setError(null);
    try {
      const r = await adminGenerateSynthetic({ count });
      setResult(r);
    } catch (err) {
      setError(err instanceof ApiError ? `Failed (${err.status})` : "Failed");
    } finally {
      setGenerating(false);
    }
  }

  if (!operator) {
    return <div className="p-6 text-sm text-gray-500">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-5xl p-6 space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Admin</h1>
          <p className="mt-0.5 text-sm text-gray-500">{operator.email}</p>
        </div>
        <div className="flex gap-3 text-sm">
          <Link href="/insights" className="text-blue-600 hover:underline">← Insights</Link>
        </div>
      </header>

      {error ? (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      ) : null}

      <section className="rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="font-medium text-gray-900">Generate synthetic logs</h2>
        <p className="mt-1 text-sm text-gray-500">
          Populates ClickHouse with plausible inference + tool-execution events
          so the dashboards have something to chew on without driving real LLM
          traffic.
        </p>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="block text-gray-700">Count</span>
            <input
              type="number"
              min={1}
              max={10000}
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
              className="mt-1 w-28 rounded-md border border-gray-300 px-2 py-1 text-sm"
            />
          </label>
          <button
            onClick={() => void handleGenerate()}
            disabled={generating}
            className="rounded-md bg-purple-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-purple-700 disabled:opacity-60"
          >
            {generating ? "Generating…" : "Generate"}
          </button>
          {result ? (
            <span className="text-sm text-gray-600">
              {result.inference_events} inference + {result.tool_events} tools
              ({result.error_events} errors)
            </span>
          ) : null}
        </div>
      </section>

      <section className="rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="font-medium text-gray-900">All users</h2>
        <ul className="mt-3 divide-y divide-gray-100 text-sm">
          {users?.length ? users.map((u) => (
            <li key={u.id} className="flex items-center justify-between py-2">
              <span className="truncate">{u.email}</span>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                u.role === "admin"
                  ? "bg-purple-100 text-purple-700"
                  : "bg-gray-100 text-gray-700"
              }`}>
                {u.role}
              </span>
            </li>
          )) : <li className="py-2 text-gray-500">No users.</li>}
        </ul>
      </section>

      <section className="rounded-lg border border-gray-200 bg-white p-4">
        <h2 className="font-medium text-gray-900">All conversations</h2>
        <ul className="mt-3 divide-y divide-gray-100 text-sm">
          {conversations?.length ? conversations.map((c) => (
            <li key={c.id} className="flex items-center justify-between py-2">
              <span className="truncate">{c.title || "(untitled)"}</span>
              <span className="text-xs text-gray-500">{c.model ?? "—"} · {c.message_count} msgs</span>
            </li>
          )) : <li className="py-2 text-gray-500">No conversations yet.</li>}
        </ul>
      </section>
    </div>
  );
}
