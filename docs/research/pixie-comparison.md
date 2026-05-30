# Inferspect agent vs Pixie — eBPF TLS tracing comparison

A focused comparison of *how the two probe libssl and move plaintext to user-space*, plus where the designs diverge. Written for the Mohit thread: "extract the code specific to the library + probing in this repo and do a comparative analysis vs Pixie."

Pixie (px.dev) is the relevant open-source reference — CNCF project, originally Pixie Labs / New Relic. Its data collector is **Stirling**. Reference: <https://docs.px.dev/about-pixie/pixie-ebpf/> and the source at `github.com/pixie-io/pixie` (`src/stirling`).

> **Verified against source** (cloned `pixie-io/pixie` @ HEAD, May 2026). The specifics below — symbols, transport, FD-correlation, library coverage — were read out of `src/stirling/source_connectors/socket_tracer/bcc_bpf/openssl_trace.c`, `socket_trace.c`, and `uprobe_manager.cc`, not from memory.

---

## 1. The probing code in *this* repo

Everything library/probing-specific lives in two files. Here's the extract.

### 1a. Which symbols we attach to

`apps/inferspect-agent/internal/kernel/loader.go`:

```go
mustAttach := []struct{ sym, prog string; ret bool }{
    {"SSL_write", "uprobe_SSL_write",       false},  // entry — capture + enforce
    {"SSL_read",  "uprobe_SSL_read_entry",  false},  // entry — stash (ssl, buf, num)
    {"SSL_read",  "uretprobe_SSL_read",     true},   // return — read decrypted bytes
}
optionalAttach := []struct{ sym, prog string; ret bool }{
    {"SSL_write_ex", "uprobe_SSL_write_ex",     false},
    {"SSL_read_ex",  "uprobe_SSL_read_ex_entry", false},
    {"SSL_read_ex",  "uretprobe_SSL_read_ex",    true},
}
```

- **uprobes** on the TLS library's public API, not kprobes on syscalls. Same fundamental choice as Pixie for TLS.
- We hook **both** the classic (`SSL_write`/`SSL_read`) and OpenSSL-3 `_ex` variants. Python's `_ssl` module, `httpx`, and recent curl call the `_ex` forms; older paths call the classic ones. `_ex` attaches are optional so a libssl 1.1 host (no `_ex` symbol) still loads.
- Symbol resolution: `link.OpenExecutable(libsslPath)` then `ex.Uprobe(symbol, prog, nil)` via cilium/ebpf. `libsslPath` is the first existing path in a candidate list (`INFERSPECT_LIBSSL_PATHS`).

### 1b. Why uprobes on the library (not the syscall)

TLS encrypts in user-space *before* the `write()` syscall. A kprobe on `sys_write` sees ciphertext. A uprobe on `SSL_write` sees **plaintext** — the buffer the application handed to OpenSSL, before it's encrypted. For `SSL_read` it's the reverse: the entry probe can't see the data yet (it hasn't been decrypted), so we stash the args and read the buffer on the **return** probe, after OpenSSL has filled it with decrypted plaintext.

### 1c. How we extract plaintext

`bpf/ssl_uprobe.c::emit_event`:

```c
__u32 cap = ((__u32)num) & (MAX_PAYLOAD - 1);   // verifier-friendly bound
ev->len = cap;
if (cap > 0 && buf != 0) {
    long r = bpf_probe_read_user(&ev->payload, cap, buf);  // copy from app memory
    ...
}
bpf_ringbuf_submit(ev, 0);
```

`bpf_probe_read_user` copies from the calling process's user-space memory into a kernel-side event struct. `SSL_read`'s return probe needs the args from entry, so we stash them in a per-task hash keyed by `pid_tgid`:

```c
struct ssl_read_args { __u64 ssl_ctx; __u64 buf; __u64 num; };
// BPF_MAP_TYPE_HASH keyed by pid_tgid, populated on entry, read on return.
```

### 1d. Kernel → user-space transport

We use a **BPF ring buffer** (`BPF_MAP_TYPE_RINGBUF`, 16 MiB):

```c
struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 24);
} events SEC(".maps");
```

User-space consumes it with `ringbuf.NewReader(eventsMap)` (cilium/ebpf). Ring buffer (5.8+) over the older perf buffer: lower memory, no per-CPU duplication, preserves ordering.

### 1e. Connection identity

We key on the **`SSL*` pointer** (the `ssl_ctx` field) — stable for the lifetime of one TLS connection — plus `(pid, tid)`. User-space reassembles HTTP per `(pid, ssl_ctx)`. We do **not** maintain a separate kprobe-driven connection map (connect/accept/close); the `SSL*` pointer is sufficient because we only care about LLM HTTPS traffic, not arbitrary socket flows.

### 1f. What's unique to us — in-kernel enforcement

This is the part Pixie does **not** have. Four BPF maps user-space populates to make the kernel *act*, not just observe:

```c
blocked_sockets       LRU_HASH  (pid<<32)|fd → 1     // legacy socket kill
blocked_ssl_contexts  LRU_HASH  SSL* → 1             // corrupt next write on this conn
blocked_pids          LRU_HASH  pid → 1              // short-TTL PID-wide block
blocked_anchors       ARRAY[8]  {len, bytes[128]}    // content-substring scan
```

On `SSL_write` entry, `enforce_if_blocked` checks these and, on a hit, calls `bpf_probe_write_user(buf, &zero, 1)` to corrupt the first byte of the outbound JSON. The server returns HTTP 400; the app sees a failed request. The `blocked_anchors` table is scanned (first 512 bytes, branchless XOR-accumulate inner loop) so we can kill by *content* without any connection identity — see `docs/PLAN.md` Phase G section.

---

## 2. Side-by-side

| Dimension | Inferspect agent | Pixie (Stirling) |
| --- | --- | --- |
| **Primary goal** | Runtime *defense* — observe **and** kill LLM traffic | Observability — observe only, never modifies traffic |
| **TLS probe type** | uprobes on libssl `SSL_write`/`SSL_read`(+`_ex`) | uprobes on TLS library API — `probe_entry/ret_SSL_write` + `probe_entry/ret_SSL_read` (hooks **both** entry and return of **both** calls) |
| **Plaintext capture** | `bpf_probe_read_user` on SSL_write entry / SSL_read return | Same mechanism. Args stashed in `active_ssl_{read,write}_args_map` (BPF_HASH keyed by `pid_tgid`) on entry, read on return |
| **Library coverage** | OpenSSL 1.1 + 3.x (libssl) | OpenSSL `libssl.so.1.1`+`libssl.so.3` (+`libcrypto`), BoringSSL, **Go `crypto/tls`**, **Node.js TLSWrap** — all via version-specific symbol-offset tables |
| **Non-TLS traffic** | Not covered (LLM APIs are all HTTPS) | kprobes on `send`/`recv`/`write`/`read` syscalls for plaintext protocols |
| **Connection tracking** | `SSL*` pointer + `(pid, ssl_ctx)` | Extracts the **socket FD from `ssl->rbio->num`** (per-version offsets) so SSL data joins a conn map keyed `(tgid, fd)`, populated by kprobes on `connect`/`accept`/`close` — gives full 5-tuple |
| **Kernel→user transport** | BPF ring buffer (16 MiB) | BPF **perf buffer** — `BPF_PERF_OUTPUT(socket_data_events)` + `.perf_submit()`; per-CPU |
| **Protocol parsing** | HTTP/1.1 reassembly in Go user-space; JSON parsed for LLM message extraction | Large protocol-parser suite in C++: HTTP, HTTP2/gRPC, MySQL, Postgres, Redis, Kafka, DNS, NATS, Cassandra, AMQP, Mongo |
| **HTTP/2** | Not yet (roadmap) | Yes — incl. gRPC over h2, with Go uprobe-based header tracing |
| **Container awareness** | Single-host VM today; resolves one libssl path | Resolves libs inside each container's mount namespace (e.g. `/usr/lib/mount/<id>/usr/lib/libssl.so.1.1`); auto-discovers across the node |
| **Symbol/offset stability** | Relies on stable `(ssl, buf, num)` ABI — unchanged across OpenSSL 1.1→3; no struct offsets needed | Maintains per-version offset tables for OpenSSL (`SSL_rbio_offset`, `RBIO_num_offset`), Go, and Node — fragile, needs updates per release. Picks FD-access strategy per OpenSSL version (`kNestedSyscall` vs offset-based) |
| **Enforcement / kill** | ✅ corrupts SSL_write in-kernel; content-anchor, SSL-ctx, PID, socket targeting | ❌ none — strictly read-only |
| **PII handling** | Regex redaction in-agent **before** uplink | Redaction/PII is a downstream concern; Stirling ships raw |
| **Footprint** | One Go binary + ~400-line BPF C; tuned for kernel 5.15 verifier ceiling | Heavyweight: Stirling is a large C++ collector, part of the full Pixie/Vizier stack (PEM, Kelvin, etc.) |
| **License / maturity** | Internal, early (Phase G) | CNCF, production-grade, years of hardening, huge protocol coverage |

---

## 3. What we deliberately do the same

1. **uprobe-on-library, not kprobe-on-syscall, for TLS.** This is *the* core Pixie technique and the reason both can read plaintext from encrypted connections without touching keys or terminating TLS. Mohit's note "we did the same thing and used the client library" is accurate — we attach to the same `SSL_write`/`SSL_read` API surface.
2. **Read-before-encrypt / read-after-decrypt timing.** Entry probe for writes (plaintext is the input arg), return probe for reads (plaintext is in the output buffer after the call).
3. **User-space protocol parsing.** Both keep BPF dumb (just copy bytes) and parse HTTP/JSON in user-space, because the verifier can't safely walk arbitrary protocol grammars.

## 4. What we do differently — and why

1. **Enforcement.** This is the whole reason Inferspect exists and the single biggest divergence. Pixie is an observability tool; it will never write to a traced buffer. We use `bpf_probe_write_user` to corrupt outbound requests so the backend can stop a hallucinating/leaking conversation in flight. That capability is what makes Ollive a *runtime defense* product rather than another telemetry pipe. (See `docs/research/agents-of-chaos.md` for why runtime defense matters.)
2. **Ring buffer over perf buffer.** We target modern kernels (5.8+) and get simpler, lower-memory, ordering-preserving transport. Pixie's perf-buffer choice predates the ring buffer and carries per-CPU overhead + ordering caveats.
3. **Narrow scope = smaller surface.** Pixie traces ~12 protocols across TLS + plaintext + Go/Node internals. We trace exactly one thing: LLM HTTPS to known providers. That lets us skip the connection-tracking kprobe layer, the per-Go-version offset tables, and the C++ parser zoo. Smaller binary, fewer moving parts, easier to ship as a single privileged daemon.
4. **On-host PII redaction.** We scrub before uplink so raw PII never crosses the network — a requirement for the "AI liability insurance" customer that Pixie has no equivalent of (it's a dev-observability tool, not a compliance product).

## 5. Where Pixie is clearly ahead (honest gaps)

- **BoringSSL / Go `crypto/tls` / Node.js coverage.** This is Pixie's hardest-won engineering — tracing statically-linked Go TLS requires reading Go runtime structs at version-specific offsets. We don't cover any of it yet; it's our Phase G.5 roadmap item ("multi-runtime SSL coverage"). Until then, Claude Code, Cursor, and any Node/Go-based LLM client are invisible to us.
- **HTTP/2 + gRPC.** Pixie parses h2 framing and gRPC. We assume HTTP/1.1; an h2-negotiating client slips past our reassembler.
- **Container/node auto-discovery.** Pixie resolves libs across every container's mount namespace on a node automatically. We attach to one libssl path on one host.
- **Protocol breadth + battle-testing.** Years of production hardening, CNCF review, a parser for nearly every wire protocol. We have one HTTP/JSON path.

## 6. One-paragraph takeaway for the thread

> We use the same fundamental eBPF technique as Pixie — uprobes on libssl's `SSL_write`/`SSL_read`, args stashed in a per-`pid_tgid` hash, plaintext read via `bpf_probe_read_user` before encryption / after decryption — so the "we did the same thing with the client library" framing is exactly correct; I verified it against Pixie's `openssl_trace.c`. Where we diverge is intent. Pixie is read-only observability with broad coverage (OpenSSL + BoringSSL/Go + Node, HTTP2/gRPC + ~12 protocols), correlates each TLS read to a socket FD pulled out of `ssl->rbio->num` via per-version offset tables, and ships over perf buffers. We are a narrow, single-purpose runtime-defense agent — LLM HTTPS only, key on the `SSL*` pointer (no offset tables to maintain), ring-buffer transport, on-host PII redaction, and crucially **in-kernel enforcement** (`bpf_probe_write_user` to kill a conversation) which Pixie does not and will not do. Pixie is the better reference for *capture breadth*; our differentiator is *acting on what we capture*. The clearest thing to borrow next is their BoringSSL/Go-TLS + Node TLSWrap tracing — that's our Phase G.5 gap, and their offset-table approach is the proven blueprint.

---

## 7. Source map for reviewers

| Concern | File |
| --- | --- |
| Which symbols, attach logic | `apps/inferspect-agent/internal/kernel/loader.go` |
| BPF programs, maps, plaintext copy, enforcement | `apps/inferspect-agent/bpf/ssl_uprobe.c` |
| HTTP reassembly (user-space) | `apps/inferspect-agent/internal/http/reassembler.go` |
| Provider identification | `apps/inferspect-agent/internal/llm/identifier.go` |
| Ring buffer consumer | `apps/inferspect-agent/internal/ringbuf/reader.go` |
| Kernel↔userspace data-plane writeup | `ARCHITECTURE.md` (eBPF agent section) |
| Design rationale + kill flow | `docs/PLAN.md` (Phase G section) |
