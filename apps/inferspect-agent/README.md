# inferspect-agent

Privileged eBPF agent that captures LLM API traffic at libssl-level on the
host and ships it to ingestion-service. Phase G.1 deliverable.

## What it does today

- Loads `bpf/ssl_uprobe.c` and attaches uprobes to `SSL_write`/`SSL_read` in
  `/usr/lib/x86_64-linux-gnu/libssl.so.3` (and 1.1 fallback).
- Reassembles HTTP/1.1 requests per `(pid, ssl_ctx)` in user-space.
- Identifies the LLM provider (OpenAI / Anthropic) by Host header.
- Computes the conversation fingerprint = `SHA256(canonicalize(system + first_user))`.
- Batches inference events to `/v1/logs` with `source="ebpf-agent"`.
- Long-polls `/v1/control/poll` for kill commands and stores them in a
  user-space deny set.

## What's intentionally a known revisit

- Kernel-side socket destruction on a fingerprint hit is logged but not yet
  wired (Phase G.2).
- HTTP/2 framing — needed for any client that negotiates h2 over ALPN.
- Response stitching from `SSL_read` events into the same `request_id` row.
- BoringSSL coverage for Node.js / Claude Code / Cursor (Phase G.5).
- Fingerprint design (what to hash, canonicalization rules) — see plan §4b.

## Running locally

```
docker compose --profile agent up -d inferspect-agent
docker logs -f inferspect-agent
```

Requires Docker Desktop on Mac (uses the Linux VM); won't run natively on
macOS or Windows hosts.

## Configuration

| Env var | Default | Notes |
| --- | --- | --- |
| `INFERSPECT_INGESTION_URL` | `http://ingestion-service:8001` | Base, agent appends `/v1/logs` and `/v1/control/*` |
| `INFERSPECT_API_KEY` | (required) | Same key the SDK uses |
| `INFERSPECT_HOST_ID` | — | Stable identity; falls back to `/var/lib/inferspect/host_id` then hostname |
| `INFERSPECT_LIBSSL_PATHS` | `/usr/lib/x86_64-linux-gnu/libssl.so.3:...` | `:`-separated list; first one that exists wins |
| `INFERSPECT_BATCH_INTERVAL` | `250ms` | Flush cadence |
| `INFERSPECT_BATCH_MAX_EVENTS` | `256` | Force-flush threshold |
| `INFERSPECT_POLL_TIMEOUT` | `60s` | Long-poll hold time |
| `INFERSPECT_BPF_OBJECT` | `/usr/local/share/inferspect/ssl_uprobe.o` | Compiled BPF ELF |
