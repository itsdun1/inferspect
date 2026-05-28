# inferspect-agent

Privileged eBPF agent that captures LLM API traffic at libssl-level on the customer host **and** accepts content-anchor kill commands from the backend. This is the daemon-only, no-customer-code-change path of the Ollive platform — companion to the chatbot SDK rather than a replacement.

For the architectural rationale see `../../docs/PLAN.md` (Phase G section). For the runtime mechanism — how the userspace Go agent talks to the kernel BPF program — see `../../ARCHITECTURE.md` (eBPF agent section). For the security-research framing that motivates runtime defense, see `../../docs/research/agents-of-chaos.md`.

## What it does

**Capture path** (kernel → userspace, asynchronous):

1. Loads `bpf/ssl_uprobe.c`; attaches uprobes to `SSL_write`, `SSL_write_ex`, `SSL_read[_ex]` in the host's libssl.
2. Per-CPU scratch buffer + ringbuf stream every plaintext SSL_write up to userspace.
3. Reassembles HTTP/1.1 requests + responses per `(pid, ssl_ctx)`.
4. Identifies the LLM provider (OpenAI / Anthropic) by Host header + SNI.
5. Computes the conversation fingerprint = `SHA256(canonicalize(system + first_user))`.
6. Maintains an in-process conversation tracker — a rolling hash chain over the message history that identifies "turn N+1 of an existing chat", so two parallel conversations that started identically still get separate Agent IDs once they diverge.
7. **Redacts PII** (email / phone / credit-card / SSN / IPv4) before any uplink. Raw PII never leaves the host.
8. Ships events to ingestion-service via batched POST `/v1/logs` with `source="ebpf-agent"`.

**Kill path** (userspace → kernel, synchronous):

1. Long-polls `/v1/control/poll` for commands.
2. On `block_fingerprint`: looks up the conversation's first user message in the **host-local** tracker (the backend sends only the fingerprint — never raw text), builds a kernel content-anchor, writes it into the `blocked_anchors` BPF map.
3. Pre-arms `blocked_ssl_contexts` for every SSL_CTX the tracker has seen for this fingerprint so the next outbound write on those connections is corrupted immediately.
4. **Layer 2 backstop**: on every captured request, scans the FULL reassembled body against armed anchors in userspace (`bytes.Index`) and pre-arms SSL_CTX on hit — catches the case where the killed text lands past the kernel's 512-byte scan window.
5. On `unblock_fingerprint`: clears every related state — policy store, SSL_CTX pre-arms, anchor slot.

When the kernel scan or pre-arm matches, `bpf_probe_write_user` corrupts the first byte of the outbound SSL_write buffer; OpenAI returns 400; chat-service surfaces 500 to its caller.

## What's intentionally a known revisit

- BoringSSL coverage for Node.js / Bun (Claude Code, Cursor, GitHub Copilot CLI). Different libssl symbols, ~50 LOC extra BPF — tracked as future work.
- HTTP/2 framing — needed for any client that negotiates h2 over ALPN. Today's flow assumes HTTP/1.1.
- Multi-runtime SSL coverage beyond libssl + BoringSSL.
- Fast non-LLM skip via per-`(socket, pid)` BPF set populated by SNI match. Today every libssl SSL_write on the host pays the uprobe overhead.
- Stronger PII tier (opt-in Presidio for customers who want NER-based detection on top of the lightweight regex).

## Running locally

The agent runs in a multipass VM (Mac developer setup) so we have real Linux kernel + libssl. Caddy on the host forwards `chat.localhost/api/chat/*` to `host.docker.internal:8000` which multipass exposes to the VM's chat-service.

```
multipass exec inferspect -- sudo systemctl restart inferspect-agent.service
multipass exec inferspect -- sudo journalctl -u inferspect-agent.service -f
```

Build native (inside the VM):

```
cd /work/inferspect-agent && bash scripts/build-native.sh
```

This compiles BPF (`clang -target bpf`) and the Go binary into `./out/`.

## Configuration

| Env var | Default | Notes |
| --- | --- | --- |
| `INFERSPECT_INGESTION_URL` | `http://ingestion-service:8001` | Base; agent appends `/v1/logs` and `/v1/control/*` |
| `INFERSPECT_API_KEY` | (required) | Same key the SDK uses |
| `INFERSPECT_HOST_ID` | — | Stable identity; falls back to `/var/lib/inferspect/host_id` then hostname |
| `INFERSPECT_LIBSSL_PATHS` | `/usr/lib/x86_64-linux-gnu/libssl.so.3:...` | `:`-separated list; first one that exists wins |
| `INFERSPECT_BATCH_INTERVAL` | `250ms` | Flush cadence |
| `INFERSPECT_BATCH_MAX_EVENTS` | `256` | Force-flush threshold |
| `INFERSPECT_POLL_TIMEOUT` | `60s` | Long-poll hold time |
| `INFERSPECT_BPF_OBJECT` | `./out/ssl_uprobe.o` | Compiled BPF ELF |

## Layout

```
apps/inferspect-agent/
├── bpf/
│   ├── ssl_uprobe.c              # uprobes + ringbuf + BPF maps + content scan
│   └── vmlinux_{aarch64,x86_64}.h
├── cmd/agent/main.go             # entrypoint: load BPF, attach uprobes, run loops
├── internal/
│   ├── config/                   # env-driven config
│   ├── downlink/                 # long-poll /v1/control/poll
│   ├── fingerprint/              # SHA256-prefix fingerprint + rolling-hash tracker
│   ├── host/                     # host_id, kernel, BTF detection
│   ├── http/                     # per-(pid,ssl_ctx) request/response reassembly
│   ├── kernel/                   # BPF loader + uprobe attach
│   ├── llm/                      # provider identification (OpenAI / Anthropic)
│   ├── policy/                   # blocked_fingerprints store + AnchorStore + PIDWindow
│   ├── redact/                   # ★ light PII redactor (email/phone/CC/SSN/IPv4) ★
│   ├── ringbuf/                  # async ringbuf reader
│   └── uplink/                   # batched POST /v1/logs
├── scripts/build-native.sh       # clang + go build
└── Dockerfile
```

The `internal/redact` and `internal/fingerprint/tracker.FirstUserText` pair are the privacy-preserving heart of the design: PII gets scrubbed before uplink, the raw bytes the kill flow needs stay only in process memory on the customer host.

## Operator UX

The agent itself has no UI. Operators interact with it via:

- `https://insights.localhost/agents` — fleet view + Kill / Unblock buttons per captured conversation
- `https://insights.localhost/enforcement` — audit log (issued kills, matched flag)
- `https://insights.localhost/insights/sessions/<id>` — per-session drilldown with a Kill button keyed on session_id

All three call insights-api → ingestion-service → agent downlink.
