"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, insightsApi } from "../../../../lib/api";
import type {
  SessionTimeline,
  SessionTimelineEntry,
} from "@ollive/web-shared";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function SessionPage({ params }: PageProps) {
  const { id } = use(params);
  const [timeline, setTimeline] = useState<SessionTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const data = await insightsApi.get<SessionTimeline>(
          `/insights/sessions/${id}`,
        );
        if (!cancelled) {
          setTimeline(data);
          setError(null);
        }
      } catch (err) {
        if (cancelled) return;
        setTimeline(null);
        setError(
          err instanceof ApiError
            ? `HTTP ${err.status}`
            : "No data yet",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <header className="border-b border-gray-200 bg-white px-6 py-4">
        <Link
          href="/insights"
          className="text-sm font-medium text-blue-600 hover:text-blue-800 transition"
        >
          ← Back to insights
        </Link>
        <h1 className="mt-2 text-lg font-semibold text-gray-900">
          Session timeline
        </h1>
        <p className="font-mono text-xs text-gray-500">{id}</p>
      </header>

      <main className="px-6 py-6">
        {loading ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : !timeline || !timeline.timeline || timeline.timeline.length === 0 ? (
          <p className="text-sm text-gray-500">{error ?? "No data yet"}</p>
        ) : (
          <ol className="relative border-l-2 border-gray-200 ml-2 space-y-4">
            {timeline.timeline.map((entry, i) => (
              <TimelineRow key={i} entry={entry} />
            ))}
          </ol>
        )}
      </main>
    </div>
  );
}

function TimelineRow({ entry }: { entry: SessionTimelineEntry }) {
  const dotColor =
    (entry.event_type ?? entry.kind) === "inference"
      ? "bg-blue-500"
      : (entry.event_type ?? entry.kind) === "tool_execution"
        ? "bg-emerald-500"
        : entry.level === "ERROR"
          ? "bg-red-500"
          : "bg-gray-400";

  return (
    <li className="ml-4">
      <span
        className={`absolute -left-[7px] mt-1.5 inline-block h-3 w-3 rounded-full ring-2 ring-white ${dotColor}`}
      />
      <div className="rounded-md border border-gray-200 bg-white p-3 shadow-sm">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-gray-500">
          <div className="flex items-center gap-2">
            <span className="font-mono">
              {new Date(entry.ts ?? entry.started_at ?? "").toLocaleString()}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
              {(entry.event_type ?? entry.kind)}
            </span>
            {entry.service ? (
              <span className="text-gray-500">{entry.service}</span>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            {entry.model ? <span>{entry.model}</span> : null}
            {entry.latency_ms != null ? (
              <span className="tabular-nums">{entry.latency_ms}ms</span>
            ) : null}
            {entry.status ? (
              <span
                className={
                  entry.status === "error"
                    ? "text-red-600"
                    : entry.status === "cancelled"
                      ? "text-gray-500"
                      : "text-emerald-700"
                }
              >
                {entry.status}
              </span>
            ) : null}
          </div>
        </div>
        {entry.message ? (
          <p className="mt-2 text-sm text-gray-800 whitespace-pre-wrap break-words">
            {entry.message}
          </p>
        ) : null}
        {entry.input_preview ? (
          <p className="mt-2 text-xs text-gray-600">
            <span className="font-semibold">Input:</span>{" "}
            <span className="whitespace-pre-wrap break-words">
              {entry.input_preview}
            </span>
          </p>
        ) : null}
        {entry.output_preview ? (
          <p className="mt-1 text-xs text-gray-600">
            <span className="font-semibold">Output:</span>{" "}
            <span className="whitespace-pre-wrap break-words">
              {entry.output_preview}
            </span>
          </p>
        ) : null}
      </div>
    </li>
  );
}
