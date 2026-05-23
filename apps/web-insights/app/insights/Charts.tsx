"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  CostPoint,
  ErrorsGroup,
  LatencyPoint,
  ThroughputPoint,
  ToolStat,
} from "@ollive/web-shared";

function shortTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const AXIS_TICK = { fontSize: 11, fill: "#6b7280" };

export function LatencyChart({ data }: { data: LatencyPoint[] }) {
  const formatted = data.map((d) => ({ ...d, bucket: shortTime(d.bucket) }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={formatted} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="bucket" tick={AXIS_TICK} />
        <YAxis tick={AXIS_TICK} unit="ms" />
        <Tooltip
          contentStyle={{ fontSize: 12, borderRadius: 6 }}
          formatter={(v) => `${Math.round(Number(v))}ms`}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Line
          type="monotone"
          dataKey="p50"
          stroke="#2563eb"
          strokeWidth={2}
          dot={false}
        />
        <Line
          type="monotone"
          dataKey="p95"
          stroke="#7c3aed"
          strokeWidth={2}
          dot={false}
        />
        <Line
          type="monotone"
          dataKey="p99"
          stroke="#dc2626"
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function ThroughputChart({ data }: { data: ThroughputPoint[] }) {
  const formatted = data.map((d) => ({ ...d, bucket: shortTime(d.bucket) }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={formatted} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="bucket" tick={AXIS_TICK} />
        <YAxis tick={AXIS_TICK} />
        <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6 }} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Line
          type="monotone"
          dataKey="req_per_min"
          name="req/min"
          stroke="#2563eb"
          strokeWidth={2}
          dot={false}
        />
        <Line
          type="monotone"
          dataKey="tokens_per_min"
          name="tokens/min"
          stroke="#7c3aed"
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function CostChart({ data }: { data: CostPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="model" tick={AXIS_TICK} />
        <YAxis tick={AXIS_TICK} unit="$" />
        <Tooltip
          contentStyle={{ fontSize: 12, borderRadius: 6 }}
          formatter={(v) => `$${Number(v).toFixed(4)}`}
        />
        <Bar dataKey="cost_usd" name="USD" fill="#2563eb" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ErrorsChart({ data }: { data: ErrorsGroup[] }) {
  // Aggregate to `${error_code} (${provider})` so each group is one bar.
  const formatted = data.map((d) => ({
    label: `${d.error_code} · ${d.provider}`,
    error_count: d.error_count,
    samples: d.samples,
  }));
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={formatted} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="label" tick={{ ...AXIS_TICK, fontSize: 10 }} angle={-15} textAnchor="end" height={50} interval={0} />
        <YAxis tick={AXIS_TICK} allowDecimals={false} />
        <Tooltip
          contentStyle={{ fontSize: 12, borderRadius: 6, maxWidth: 320 }}
          formatter={(v) => `${Number(v)} errors`}
          labelFormatter={(label) => label as string}
        />
        <Bar dataKey="error_count" name="Errors" fill="#dc2626" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ToolsChart({ data }: { data: ToolStat[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="tool_name" tick={{ ...AXIS_TICK, fontSize: 10 }} angle={-15} textAnchor="end" height={50} interval={0} />
        <YAxis tick={AXIS_TICK} allowDecimals={false} />
        <Tooltip
          contentStyle={{ fontSize: 12, borderRadius: 6 }}
          formatter={(v, n) => [`${Number(v)}`, n as string]}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="call_count" name="Calls" fill="#7c3aed" radius={[4, 4, 0, 0]} />
        <Bar dataKey="error_count" name="Errors" fill="#dc2626" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
