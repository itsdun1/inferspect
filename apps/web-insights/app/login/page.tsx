"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, login } from "../../lib/api";

// Operator-side login. Styled distinctly (slate/violet) from the chat-side
// login so operators don't confuse this with the end-user surface. There is
// no /register here — operators are provisioned via env-bootstrap
// (CONSOLE_BOOTSTRAP_EMAIL / CONSOLE_BOOTSTRAP_PASSWORD).
export default function OperatorLoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email.trim(), password);
      router.push("/insights");
      router.refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setError("Invalid operator credentials.");
      } else {
        setError("Login failed. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center px-4 bg-slate-900 min-h-screen">
      <div className="w-full max-w-sm rounded-lg border border-slate-700 bg-slate-800 p-6 shadow-xl">
        <div className="mb-4 flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-full bg-violet-400" />
          <span className="text-xs uppercase tracking-wider text-violet-300">
            Operator Console
          </span>
        </div>
        <h1 className="text-xl font-semibold text-white">Sign in</h1>
        <p className="mt-1 text-sm text-slate-400">
          Restricted to Ollive operators. End-users should use the chat surface.
        </p>
        <form onSubmit={handleSubmit} className="mt-5 space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium text-slate-300">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white shadow-sm transition focus:border-violet-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
              placeholder="operator@ollive.local"
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-sm font-medium text-slate-300">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white shadow-sm transition focus:border-violet-500 focus:outline-none focus:ring-1 focus:ring-violet-500"
            />
          </div>
          {error ? <p className="text-sm text-red-400">{error}</p> : null}
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded-md bg-violet-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-violet-700 focus:outline-none focus:ring-2 focus:ring-violet-500 focus:ring-offset-2 focus:ring-offset-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
