"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  getAgents,
  getHostFingerprints,
  killOnHost,
  type AgentRow,
  type HostFingerprint,
} from "../../lib/api";

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getAgents();
      setAgents(data.agents ?? []);
      setError(null);
    } catch (err) {
      setAgents([]);
      setError(
        err instanceof ApiError
          ? `HTTP ${err.status}`
          : "Failed to load agent fleet",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <header className="border-b border-gray-200 bg-white px-6 py-4">
        <Link
          href="/insights"
          className="text-sm font-medium text-blue-600 hover:text-blue-800 transition"
        >
          ← Back to insights
        </Link>
        <div className="mt-2 flex items-baseline justify-between">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">
              Connected agents
            </h1>
            <p className="text-xs text-gray-500">
              Hosts that have emitted eBPF-agent observations in the last hour.
              Click a row to see captured conversations and kill any.
            </p>
          </div>
          <Link
            href="/enforcement"
            className="text-sm font-medium text-blue-600 hover:text-blue-800 transition"
          >
            Enforcement log →
          </Link>
        </div>
      </header>

      <main className="px-6 py-6">
        {loading && agents.length === 0 ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : agents.length === 0 ? (
          <div className="rounded border border-dashed border-gray-300 p-8 text-center">
            <p className="text-sm text-gray-600">
              {error ?? "No agents have heartbeated in the last hour."}
            </p>
            <p className="mt-2 text-xs text-gray-500">
              Start the agent locally with{" "}
              <code className="rounded bg-gray-100 px-1">
                docker compose --profile agent up -d
              </code>
              .
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto rounded border border-gray-200 bg-white">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-600">
                <tr>
                  <th className="px-4 py-3 text-left">Host</th>
                  <th className="px-4 py-3 text-left">Status</th>
                  <th className="px-4 py-3 text-left">Last seen</th>
                  <th className="px-4 py-3 text-right">Events (1h)</th>
                  <th className="px-4 py-3 text-right">PIDs</th>
                  <th className="px-4 py-3 text-right">Providers</th>
                  <th className="px-4 py-3 text-left">Container</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 text-sm">
                {agents.map((a) => (
                  <AgentRowDisplay
                    key={a.host_id}
                    agent={a}
                    open={expanded === a.host_id}
                    onToggle={() =>
                      setExpanded((cur) =>
                        cur === a.host_id ? null : a.host_id,
                      )
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}

function AgentRowDisplay({
  agent,
  open,
  onToggle,
}: {
  agent: AgentRow;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className="cursor-pointer hover:bg-gray-50" onClick={onToggle}>
        <td className="px-4 py-2 font-mono text-xs">
          <span className="inline-flex items-center gap-1">
            <span className="text-gray-400">{open ? "▾" : "▸"}</span>
            {agent.host_id}
          </span>
        </td>
        <td className="px-4 py-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
              agent.is_live
                ? "bg-emerald-100 text-emerald-700"
                : "bg-gray-100 text-gray-600"
            }`}
          >
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                agent.is_live ? "bg-emerald-500" : "bg-gray-400"
              }`}
            />
            {agent.is_live ? "live" : "idle"}
          </span>
        </td>
        <td className="px-4 py-2 text-xs text-gray-600">
          {agent.last_seen ? new Date(agent.last_seen).toLocaleString() : "—"}
        </td>
        <td className="px-4 py-2 text-right tabular-nums">
          {agent.event_count.toLocaleString()}
        </td>
        <td className="px-4 py-2 text-right tabular-nums">
          {agent.distinct_pids}
        </td>
        <td className="px-4 py-2 text-right tabular-nums">
          {agent.distinct_providers}
        </td>
        <td className="px-4 py-2 font-mono text-xs text-gray-600">
          {agent.container_id || "—"}
        </td>
      </tr>
      {open ? <FingerprintRow hostId={agent.host_id} /> : null}
    </>
  );
}

function FingerprintRow({ hostId }: { hostId: string }) {
  const [fps, setFps] = useState<HostFingerprint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getHostFingerprints(hostId, { windowHours: 1, limit: 50 });
      setFps(data.fingerprints ?? []);
      setError(null);
    } catch (err) {
      setFps([]);
      setError(err instanceof ApiError ? `HTTP ${err.status}` : "Failed to load");
    } finally {
      setLoading(false);
    }
  }, [hostId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <tr>
      <td colSpan={7} className="bg-gray-50 px-4 py-3">
        {loading && fps.length === 0 ? (
          <p className="text-xs text-gray-500">Loading conversations…</p>
        ) : fps.length === 0 ? (
          <p className="text-xs text-gray-500">
            {error ?? "No conversations captured on this host in the last hour."}
          </p>
        ) : (
          <table className="min-w-full text-xs">
            <thead className="text-gray-600">
              <tr>
                <th className="px-2 py-1 text-left">Fingerprint</th>
                <th className="px-2 py-1 text-left">Provider/Model</th>
                <th className="px-2 py-1 text-right">Turns</th>
                <th className="px-2 py-1 text-left">First seen</th>
                <th className="px-2 py-1 text-left">Last seen</th>
                <th className="px-2 py-1 text-left">Preview</th>
                <th className="px-2 py-1 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {fps.map((fp) => (
                <KillableFingerprint
                  key={fp.fingerprint}
                  hostId={hostId}
                  fp={fp}
                  onKilled={load}
                />
              ))}
            </tbody>
          </table>
        )}
      </td>
    </tr>
  );
}

function KillableFingerprint({
  hostId,
  fp,
  onKilled,
}: {
  hostId: string;
  fp: HostFingerprint;
  onKilled: () => void;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ok" }
    | { status: "error"; message: string }
  >({ status: "idle" });

  const onKill = async () => {
    if (
      !confirm(
        `Block all conversations matching this fingerprint?\n\n${fp.fingerprint.slice(0, 16)}…\n\nNext request matching this prefix will be disrupted in-kernel.`,
      )
    ) {
      return;
    }
    setState({ status: "loading" });
    try {
      await killOnHost(hostId, {
        fingerprint: fp.fingerprint,
        reason: "operator_kill_from_agents_page",
      });
      setState({ status: "ok" });
      setTimeout(onKilled, 500);
    } catch (err) {
      setState({
        status: "error",
        message:
          err instanceof ApiError
            ? typeof err.body === "object" && err.body && "detail" in err.body
              ? String((err.body as { detail?: unknown }).detail)
              : `HTTP ${err.status}`
            : "Kill failed",
      });
    }
  };

  return (
    <tr className="border-t border-gray-200 hover:bg-white">
      <td className="px-2 py-1 font-mono">{fp.fingerprint.slice(0, 16)}…</td>
      <td className="px-2 py-1">
        {fp.provider}/{fp.model || "—"}
      </td>
      <td className="px-2 py-1 text-right tabular-nums">{fp.request_count}</td>
      <td className="px-2 py-1 text-gray-600">
        {fp.first_seen ? new Date(fp.first_seen).toLocaleString() : "—"}
      </td>
      <td className="px-2 py-1 text-gray-600">
        {fp.last_seen ? new Date(fp.last_seen).toLocaleString() : "—"}
      </td>
      <td className="px-2 py-1 text-gray-700 max-w-md truncate" title={fp.preview}>
        {fp.preview || "—"}
      </td>
      <td className="px-2 py-1 text-right">
        {state.status === "ok" ? (
          <span className="text-xs text-emerald-700">Killed</span>
        ) : (
          <button
            type="button"
            onClick={onKill}
            disabled={state.status === "loading"}
            className="rounded-md bg-red-600 px-2 py-0.5 text-xs font-medium text-white shadow-sm hover:bg-red-700 disabled:opacity-50"
          >
            {state.status === "loading" ? "…" : "Kill"}
          </button>
        )}
        {state.status === "error" ? (
          <p className="mt-0.5 text-[10px] text-red-700">{state.message}</p>
        ) : null}
      </td>
    </tr>
  );
}
