"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ApiError,
  getEnforcementEvents,
  type EnforcementEvent,
} from "../../lib/api";

export default function EnforcementPage() {
  const [events, setEvents] = useState<EnforcementEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [windowHours, setWindowHours] = useState(24);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getEnforcementEvents({ windowHours });
      setEvents(data.events ?? []);
      setError(null);
    } catch (err) {
      setEvents([]);
      setError(
        err instanceof ApiError
          ? `HTTP ${err.status}`
          : "Failed to load enforcement log",
      );
    } finally {
      setLoading(false);
    }
  }, [windowHours]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      <header className="border-b border-gray-200 bg-white px-6 py-4">
        <Link
          href="/agents"
          className="text-sm font-medium text-blue-600 hover:text-blue-800 transition"
        >
          ← Back to agents
        </Link>
        <div className="mt-2 flex items-baseline justify-between">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">
              Enforcement log
            </h1>
            <p className="text-xs text-gray-500">
              Every kill the operator console has issued.
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-gray-600">
            <label htmlFor="window">Window</label>
            <select
              id="window"
              className="rounded border border-gray-300 px-2 py-1"
              value={windowHours}
              onChange={(e) => setWindowHours(Number(e.target.value))}
            >
              <option value={1}>1h</option>
              <option value={6}>6h</option>
              <option value={24}>24h</option>
              <option value={24 * 7}>7d</option>
            </select>
          </div>
        </div>
      </header>

      <main className="px-6 py-6">
        {loading && events.length === 0 ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : events.length === 0 ? (
          <p className="text-sm text-gray-500">
            {error ?? "No kills issued in this window."}
          </p>
        ) : (
          <div className="overflow-x-auto rounded border border-gray-200 bg-white">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-600">
                <tr>
                  <th className="px-4 py-3 text-left">When</th>
                  <th className="px-4 py-3 text-left">Host</th>
                  <th className="px-4 py-3 text-left">Command</th>
                  <th className="px-4 py-3 text-left">Fingerprint</th>
                  <th className="px-4 py-3 text-left">Reason</th>
                  <th className="px-4 py-3 text-left">Source</th>
                  <th className="px-4 py-3 text-left">Matched</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 text-sm">
                {events.map((e, i) => (
                  <tr key={`${e.timestamp}-${i}`} className="hover:bg-gray-50">
                    <td className="px-4 py-2 text-xs text-gray-600">
                      {new Date(e.timestamp).toLocaleString()}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs">{e.host_id}</td>
                    <td className="px-4 py-2">{e.command}</td>
                    <td className="px-4 py-2 font-mono text-xs">
                      {e.fingerprint.slice(0, 12)}…
                    </td>
                    <td className="px-4 py-2 text-xs">{e.reason}</td>
                    <td className="px-4 py-2 text-xs">{e.source}</td>
                    <td className="px-4 py-2 text-xs">
                      {e.matched ? (
                        <span className="text-emerald-700">yes</span>
                      ) : (
                        <span className="text-gray-500">pending</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
