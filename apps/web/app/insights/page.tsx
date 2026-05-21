"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  insightsApi,
  type CostPoint,
  type ErrorsGroup,
  type LatencyPoint,
  type SessionTimeline,
  type SessionTimelineEntry,
  type ThroughputPoint,
  type ToolStat,
  type TopConversation,
} from "../../lib/api";
import {
  CostChart,
  ErrorsChart,
  LatencyChart,
  ThroughputChart,
  ToolsChart,
} from "./Charts";

interface FetchState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

function emptyState<T>(): FetchState<T> {
  return { data: null, error: null, loading: true };
}

const WINDOWS = [
  { value: "15m", label: "Last 15m" },
  { value: "1h", label: "Last 1h" },
  { value: "6h", label: "Last 6h" },
  { value: "24h", label: "Last 24h" },
  { value: "7d", label: "Last 7d" },
  { value: "30d", label: "Last 30d" },
] as const;

export default function InsightsPage() {
  const [windowSel, setWindowSel] = useState<string>("1h");
  const [latency, setLatency] = useState<FetchState<LatencyPoint[]>>(emptyState());
  const [throughput, setThroughput] = useState<FetchState<ThroughputPoint[]>>(
    emptyState(),
  );
  const [cost, setCost] = useState<FetchState<CostPoint[]>>(emptyState());
  const [top, setTop] = useState<FetchState<TopConversation[]>>(emptyState());
  const [errors, setErrors] = useState<FetchState<ErrorsGroup[]>>(emptyState());
  const [tools, setTools] = useState<FetchState<ToolStat[]>>(emptyState());
  const [drillId, setDrillId] = useState<string | null>(null);
  const [drillData, setDrillData] = useState<FetchState<SessionTimeline>>(emptyState());

  useEffect(() => {
    // Re-fetch whenever the time window changes. The insights-api returns
    // wrapped objects ({buckets|by_group|conversations: [...]}); unwrap to
    // the bare array each chart component expects.
    void load(async () => {
      const r = await insightsApi.get<{ buckets: LatencyPoint[] }>(
        `/insights/latency?window=${windowSel}&group=model`,
      );
      return r.buckets ?? [];
    }, setLatency);
    void load(async () => {
      const r = await insightsApi.get<{ buckets: ThroughputPoint[] }>(
        `/insights/throughput?window=${windowSel}`,
      );
      return r.buckets ?? [];
    }, setThroughput);
    void load(async () => {
      const r = await insightsApi.get<{ by_group: CostPoint[] }>(
        `/insights/cost?window=${windowSel}&group=model`,
      );
      return r.by_group ?? [];
    }, setCost);
    void load(async () => {
      const r = await insightsApi.get<{ conversations: TopConversation[] }>(
        `/insights/top-conversations?metric=latency&limit=20&window=${windowSel}`,
      );
      return r.conversations ?? [];
    }, setTop);
    void load(async () => {
      const r = await insightsApi.get<{ groups: ErrorsGroup[] }>(
        `/insights/errors?window=${windowSel}`,
      );
      return r.groups ?? [];
    }, setErrors);
    void load(async () => {
      const r = await insightsApi.get<{ tools: ToolStat[] }>(
        `/insights/tools?window=${windowSel}`,
      );
      return r.tools ?? [];
    }, setTools);
  }, [windowSel]);

  // Drill-down fetch when user clicks a conversation row.
  useEffect(() => {
    if (!drillId) {
      setDrillData(emptyState());
      return;
    }
    let cancelled = false;
    setDrillData({ data: null, error: null, loading: true });
    insightsApi
      .get<SessionTimeline>(`/insights/sessions/${drillId}`)
      .then((data) => {
        if (!cancelled) setDrillData({ data, error: null, loading: false });
      })
      .catch((err) => {
        if (cancelled) return;
        setDrillData({
          data: null,
          error: err instanceof ApiError ? `HTTP ${err.status}` : "Failed",
          loading: false,
        });
      });
    return () => { cancelled = true; };
  }, [drillId]);

  const windowLabel = WINDOWS.find((w) => w.value === windowSel)?.label ?? windowSel;

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <header className="border-b border-gray-200 bg-white px-6 py-4 flex items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">Insights</h1>
          <p className="text-xs text-gray-500">Live LLM telemetry from ClickHouse.</p>
        </div>
        <div className="flex items-center gap-3">
          <label htmlFor="window" className="text-xs text-gray-500">
            Window
          </label>
          <select
            id="window"
            value={windowSel}
            onChange={(e) => setWindowSel(e.target.value)}
            className="rounded-md border border-gray-300 bg-white px-2 py-1 text-sm shadow-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>
                {w.label}
              </option>
            ))}
          </select>
          <Link
            href="/chat"
            className="text-sm font-medium text-blue-600 hover:text-blue-800 transition"
          >
            ← Back to chat
          </Link>
        </div>
      </header>

      <main className="px-6 py-6 space-y-6">
        <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <ChartCard
            title="Latency (p50 / p95 / p99)"
            subtitle={windowLabel}
            state={latency}
            render={(data) => <LatencyChart data={data} />}
          />
          <ChartCard
            title="Throughput"
            subtitle={`Requests / min · ${windowLabel}`}
            state={throughput}
            render={(data) => <ThroughputChart data={data} />}
          />
          <ChartCard
            title="Cost per model"
            subtitle={`USD · ${windowLabel}`}
            state={cost}
            render={(data) => <CostChart data={data} />}
          />
        </section>

        <section className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <ChartCard
            title="Errors by code"
            subtitle={`Counts · ${windowLabel}`}
            state={errors}
            render={(data) => <ErrorsPanel data={data} />}
          />
          <ChartCard
            title="Tool usage"
            subtitle={`Calls & error rate · ${windowLabel}`}
            state={tools}
            render={(data) => <ToolsPanel data={data} />}
          />
        </section>

        <section className="rounded-lg border border-gray-200 bg-white">
          <div className="border-b border-gray-200 px-4 py-3">
            <h2 className="text-sm font-semibold text-gray-900">
              Recent sessions
            </h2>
            <p className="text-xs text-gray-500">Click a row to view its timeline.</p>
          </div>
          <TopConversationsTable
            state={top}
            onSelect={(id) => setDrillId(id)}
          />
        </section>
      </main>

      {drillId ? (
        <SessionDrillDownModal
          drillId={drillId}
          state={drillData}
          onClose={() => setDrillId(null)}
        />
      ) : null}
    </div>
  );
}

function ErrorsPanel({ data }: { data: ErrorsGroup[] }) {
  if (!data.length) {
    return <p className="text-sm text-gray-500 px-2 py-6">No errors in this window 🎉</p>;
  }
  return (
    <div className="h-full flex flex-col">
      <div className="h-44 shrink-0">
        <ErrorsChart data={data} />
      </div>
      <div className="mt-2 max-h-32 overflow-y-auto text-xs space-y-1">
        {data.slice(0, 6).map((g, i) => (
          <div key={i} className="flex items-start gap-2 px-1">
            <span className="font-mono text-red-700 shrink-0">
              {g.error_code}
            </span>
            <span className="text-gray-500 shrink-0">·</span>
            <span className="text-gray-500 shrink-0">{g.provider}</span>
            <span className="text-gray-700 truncate" title={g.samples[0]}>
              — {g.samples[0]?.slice(0, 80) || "(no sample)"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ToolsPanel({ data }: { data: ToolStat[] }) {
  if (!data.length) {
    return <p className="text-sm text-gray-500 px-2 py-6">No tool calls in this window.</p>;
  }
  return (
    <div className="h-full flex flex-col">
      <div className="h-44 shrink-0">
        <ToolsChart data={data} />
      </div>
      <div className="mt-2 max-h-32 overflow-y-auto text-xs">
        <table className="w-full">
          <thead className="text-gray-500">
            <tr>
              <th className="text-left font-medium">Tool</th>
              <th className="text-right font-medium">Calls</th>
              <th className="text-right font-medium">p50 ms</th>
              <th className="text-right font-medium">p95 ms</th>
              <th className="text-right font-medium">Err %</th>
            </tr>
          </thead>
          <tbody>
            {data.map((t) => (
              <tr key={t.tool_name} className="border-t border-gray-100">
                <td className="py-1 font-mono">{t.tool_name}</td>
                <td className="py-1 text-right tabular-nums">{t.call_count}</td>
                <td className="py-1 text-right tabular-nums">{Math.round(t.p50_latency_ms)}</td>
                <td className="py-1 text-right tabular-nums">{Math.round(t.p95_latency_ms)}</td>
                <td className="py-1 text-right tabular-nums">{(t.error_rate * 100).toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SessionDrillDownModal({
  drillId,
  state,
  onClose,
}: {
  drillId: string;
  state: FetchState<SessionTimeline>;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl max-h-[85vh] overflow-hidden rounded-lg bg-white shadow-xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-900">Session timeline</h3>
            <p className="font-mono text-xs text-gray-500">{drillId}</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-gray-600 hover:bg-gray-100"
          >
            ✕ Close
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
          {state.loading ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : state.error ? (
            <p className="text-sm text-red-600">{state.error}</p>
          ) : !state.data || state.data.timeline.length === 0 ? (
            <p className="text-sm text-gray-500">No events for this session.</p>
          ) : (
            <>
              <div className="text-xs text-gray-500">
                {state.data.inference_count} inference · {state.data.tool_count} tool calls
              </div>
              <ol className="relative border-l-2 border-gray-200 ml-2 space-y-3">
                {state.data.timeline.map((entry, i) => (
                  <ModalTimelineRow key={i} entry={entry} />
                ))}
              </ol>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ModalTimelineRow({ entry }: { entry: SessionTimelineEntry }) {
  const kind = entry.event_type ?? entry.kind ?? "inference";
  const dotColor =
    kind === "inference" ? "bg-blue-500"
    : kind === "tool_execution" ? "bg-emerald-500"
    : "bg-gray-400";
  return (
    <li className="ml-4">
      <span className={`absolute -left-[7px] mt-1.5 inline-block h-3 w-3 rounded-full ring-2 ring-white ${dotColor}`} />
      <div className="rounded-md border border-gray-200 bg-white p-3 shadow-sm">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-gray-500">
          <div className="flex items-center gap-2">
            <span className="font-mono">
              {new Date(entry.ts ?? entry.started_at ?? "").toLocaleString()}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
              {kind}
            </span>
            {entry.model ? <span className="text-gray-500">{entry.model}</span> : null}
          </div>
          <div className="flex items-center gap-2">
            {entry.latency_ms != null ? (
              <span className="tabular-nums">{entry.latency_ms}ms</span>
            ) : null}
            {entry.status ? (
              <span className={
                entry.status === "error" ? "text-red-600"
                : entry.status === "cancelled" ? "text-gray-500"
                : "text-emerald-700"
              }>
                {entry.status}
              </span>
            ) : null}
          </div>
        </div>
        {entry.input_preview ? (
          <p className="mt-2 text-xs">
            <span className="font-semibold text-gray-600">In:</span>{" "}
            <span className="text-gray-700 whitespace-pre-wrap break-words">{entry.input_preview}</span>
          </p>
        ) : null}
        {entry.output_preview ? (
          <p className="mt-1 text-xs">
            <span className="font-semibold text-gray-600">Out:</span>{" "}
            <span className="text-gray-700 whitespace-pre-wrap break-words">{entry.output_preview}</span>
          </p>
        ) : null}
      </div>
    </li>
  );
}

async function load<T>(
  fetcher: () => Promise<T>,
  setter: React.Dispatch<React.SetStateAction<FetchState<T>>>,
) {
  setter({ data: null, error: null, loading: true });
  try {
    const data = await fetcher();
    setter({ data, error: null, loading: false });
  } catch (err) {
    const error =
      err instanceof ApiError
        ? `HTTP ${err.status}`
        : "No data yet";
    setter({ data: null, error, loading: false });
  }
}

function ChartCard<T>({
  title,
  subtitle,
  state,
  render,
}: {
  title: string;
  subtitle: string;
  state: FetchState<T[]>;
  render: (data: T[]) => React.ReactNode;
}) {
  const hasData = state.data && state.data.length > 0;
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        <span className="text-xs text-gray-500">{subtitle}</span>
      </div>
      <div className="mt-3 h-64">
        {state.loading ? (
          <div className="flex h-full items-center justify-center text-xs text-gray-400">
            Loading…
          </div>
        ) : hasData ? (
          render(state.data as T[])
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-gray-400">
            {state.error ?? "No data yet"}
          </div>
        )}
      </div>
    </div>
  );
}

function TopConversationsTable({
  state,
  onSelect,
}: {
  state: FetchState<TopConversation[]>;
  onSelect?: (conversationId: string) => void;
}) {
  if (state.loading) {
    return (
      <p className="px-4 py-6 text-sm text-gray-500">Loading sessions…</p>
    );
  }
  if (!state.data || state.data.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-gray-500">
        {state.error ?? "No data yet"}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-gray-200 text-sm">
        <thead className="bg-gray-50">
          <tr>
            <Th>Conversation</Th>
            <Th>Model</Th>
            <Th className="text-right">Requests</Th>
            <Th className="text-right">Avg latency (ms)</Th>
            <Th className="text-right">Tokens</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {state.data.map((row) => (
            <tr
              key={row.conversation_id}
              className="cursor-pointer hover:bg-blue-50/60 transition"
              onClick={() => onSelect?.(row.conversation_id)}
            >
              <Td>
                <span className="font-mono text-xs text-blue-700">
                  {row.conversation_id.slice(0, 8)}…
                </span>
              </Td>
              <Td className="text-gray-700">{row.model ?? "—"}</Td>
              <Td className="text-right tabular-nums">{row.req_count}</Td>
              <Td className="text-right tabular-nums">
                {row.metric_value.toFixed(0)}
              </Td>
              <Td className="text-right tabular-nums">{row.tokens ?? 0}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <th
      className={`px-4 py-2 text-left text-xs font-semibold text-gray-600 ${className ?? ""}`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <td className={`px-4 py-2 ${className ?? ""}`}>{children}</td>;
}
