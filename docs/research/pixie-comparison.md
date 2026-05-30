# Inferspect agent vs Pixie — eBPF TLS tracing comparison

A focused comparison of *how the two probe libssl and move plaintext to user-space*, plus where the designs diverge. Scope: extract the library/probing-specific code in the Inferspect agent and compare it, at both the architecture and code level, against Pixie's eBPF TLS tracer.

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

1. **uprobe-on-library, not kprobe-on-syscall, for TLS.** This is *the* core Pixie technique and the reason both can read plaintext from encrypted connections without touching keys or terminating TLS. We attach to the same `SSL_write`/`SSL_read` API surface — confirmed against Pixie's `openssl_trace.c`.
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

## 6. One-paragraph takeaway

> Inferspect uses the same fundamental eBPF technique as Pixie — uprobes on libssl's `SSL_write`/`SSL_read`, args stashed in a per-`pid_tgid` hash, plaintext read via `bpf_probe_read_user` before encryption / after decryption (verified against Pixie's `openssl_trace.c`). Where the two diverge is intent. Pixie is read-only observability with broad coverage (OpenSSL + BoringSSL/Go + Node, HTTP2/gRPC + ~12 protocols), correlates each TLS read to a socket FD pulled out of `ssl->rbio->num` via per-version offset tables, and ships over perf buffers. Inferspect is a narrow, single-purpose runtime-defense agent — LLM HTTPS only, keys on the `SSL*` pointer (no offset tables to maintain), ring-buffer transport, on-host PII redaction, and crucially **in-kernel enforcement** (`bpf_probe_write_user` to kill a conversation) which Pixie does not and will not do. Pixie is the better reference for *capture breadth*; Inferspect's differentiator is *acting on what it captures*. The clearest thing to borrow next is their BoringSSL/Go-TLS + Node TLSWrap tracing — the Phase G.5 gap — where their offset-table approach is the proven blueprint.

---

## 7. Code-level deep dive (read both side by side)

Based on a full read of Pixie's `openssl_trace.c`, `socket_trace.c`, and `node_openssl_trace.c`. Here's how the two implementations differ line-for-line.

### 8a. Probe surface — 6 programs vs ~12+

**Ours** (`loader.go`): 6 uprobe programs — `SSL_write` entry, `SSL_read` entry+return, and the `_ex` variants of each (optional). One job each: capture, and for SSL_write, enforce.

**Pixie** (`openssl_trace.c`): **12 SSL probe entrypoints** plus Node-specific ones. For each of write/read it ships *two complete FD-resolution strategies*, each hooking entry **and** return:

- `probe_entry_SSL_write` / `probe_ret_SSL_write` — "symaddrs" strategy
- `probe_entry_SSL_write_syscall_fd_access` / `probe_ret_..._syscall_fd_access` — "nested syscall" strategy
- ditto for read, ditto for `_ex`

Why so many: Pixie *must* recover the socket FD to correlate TLS plaintext with its connection-tracking layer. We don't — we key on the `SSL*` pointer and never need the FD. That single decision erases half their probe surface.

### 8b. The FD problem Pixie has and we don't

Pixie's `get_fd()` tries three things in order (`openssl_trace.c:96`):

```c
fd = get_fd_node(tgid, ssl);       // node: FD from a separate TLSWrap uprobe
if (fd > 2) return fd;
fd = get_fd_symaddrs(tgid, ssl);   // ssl->rbio->num via per-version offsets
if (fd > 2) return fd;
return kInvalidFD;
```

`get_fd_symaddrs` walks raw struct memory:

```c
// Extract FD via ssl->rbio->num.
const void** rbio_ptr_addr = ssl + symaddrs->SSL_rbio_offset;
const void* rbio_ptr = *rbio_ptr_addr;
const int* rbio_num_addr = rbio_ptr + symaddrs->RBIO_num_offset;
const int rbio_num = *rbio_num_addr;
return rbio_num;
```

Those offsets (`SSL_rbio_offset`, `RBIO_num_offset`) are populated **per OpenSSL version** from user-space (`openssl_symaddrs_map`, keyed by TGID). When that fails (newer OpenSSL/BoringSSL where the layout is opaque), they fall back to the **nested-syscall** strategy: on SSL_write entry they seed `ssl_user_space_call_map[pid_tgid]`, a kprobe on the real `write`/`sendto` syscall fired *during* SSL_write records the FD into it, and SSL_write's return probe reads it back — with `mismatched_fds` detection for when the heuristic is wrong.

**This is the single biggest complexity asymmetry.** Pixie carries version-keyed struct-offset tables + a nested-syscall fallback + mismatch detection, all to get an FD. Our `SSL*`-as-key approach needs none of it — at the cost of not having the 5-tuple (which we don't need for LLM-only traffic).

### 8c. Plaintext copy — masking trick vs volatile-asm trick

Both fight the same enemy: the BPF verifier rejecting a variable-length `bpf_probe_read`. Different workarounds.

**Ours** (`emit_event`): one power-of-two mask, one copy, one event.

```c
__u32 cap = ((__u32)num) & (MAX_PAYLOAD - 1);   // proves 0 ≤ cap < 8192 in one op
bpf_probe_read_user(&ev->payload, cap, buf);
bpf_ringbuf_submit(ev, 0);
```

Trade-off we documented: if `num` is an exact multiple of 8192 the cap collapses to 0 (we lose that one capture; `total` still records true size). Simple, one shot, ≤8 KiB.

**Pixie** (`perf_submit_buf`): handles up to **30 KiB** (`MAX_MSG_SIZE`) and chunks larger payloads across multiple perf events via an unrolled `CHUNK_LIMIT` loop (`perf_submit_wrapper`). To get there past the 4.14 verifier they resort to:

```c
size_t buf_size_minus_1 = buf_size - 1;
asm volatile("" : "+r"(buf_size_minus_1) :);   // stop clang optimizing away verifier hints
buf_size = buf_size_minus_1 + 1;
if (buf_size_minus_1 < MAX_MSG_SIZE) { bpf_probe_read(&event->msg, buf_size, buf); ... }
else if (buf_size_minus_1 < 0x7fffffff) { bpf_probe_read(&event->msg, MAX_MSG_SIZE, buf); ... }
```

That `asm volatile` is a famous Pixie hack — clang was "too smart" and optimized away the `if` hints the verifier needed, so they launder the variable through inline asm to force the verifier to re-reason about its bounds. We avoid the whole class of problem by capping smaller and never chunking.

### 8d. Capture vs enforce — the structural difference

Pixie's probes are pure observers. `probe_entry_SSL_write` does exactly one mutation to shared state: `set_conn_as_ssl(tgid, fd, ...)` — marks the connection so the *syscall-level* tracer doesn't double-capture the ciphertext. It never touches the application's buffer.

Ours does the same capture, then adds the thing Pixie structurally refuses to do:

```c
// enforce_if_blocked, on a hit:
bpf_probe_write_user((void *)buf, &zero, 1);   // corrupt the outbound request
```

`bpf_probe_write_user` is gated behind `CAP_SYS_ADMIN` and is exactly the helper an observability tool would never call. It's our entire product thesis in one line.

### 8e. Dedup between layers — a problem we don't have

Because Pixie traces **both** the TLS library (plaintext) **and** the raw syscalls (everything), it has to prevent counting the same bytes twice. That's what `set_conn_as_ssl` is for — once a connection is known-TLS, the syscall tracer suppresses its (encrypted) bytes and lets the uprobe layer own it. We only ever have the uprobe layer, so there's no dedup machinery at all.

### 8f. What this means

Pixie is a *general-purpose, multi-runtime, multi-protocol* observability engine, and its code shows it: version-keyed offset tables, dual FD strategies, nested-syscall detection, chunked 30 KiB submission, layer dedup, Node TLSWrap handling. Every one of those is the right call for "trace anything on any node" — and every one is complexity we get to skip by being *LLM-HTTPS-only and enforcement-first*. Our `ssl_uprobe.c` is ~400 lines total including the kill path; their socket tracer is several thousand across many files.

The lesson for our roadmap: when we add BoringSSL/Go/Node coverage (Phase G.5), we inherit Pixie's hard problems — the FD/offset tables and the Node TLSWrap dance are unavoidable there. Their code is the reference implementation to study before we write ours.

## 8. Build-vs-fork: should we extend Pixie instead of maintaining our own agent?

A reasonable question given how much capture machinery Pixie already has: rather than grow our own agent, fork Pixie and add the conversation-identity, PII, and kill features on top. Short answer — **technically possible, but the wrong trade for this product.** The detail:

### What would port over cleanly

All the *user-space* logic sits on top of capture and would drop onto Pixie without much friction:

- **Conversation identity** (content-prefix fingerprint + rolling-hash tracker) — user-space, drop-in.
- **On-host PII redaction** — user-space, drop-in.
- **Backend control plane** (long-poll downlink, anchor table) — user-space, drop-in.

And Pixie's capture is genuinely broader than ours (more runtimes, more protocols), so for the *watching* half, extending it is a real head start.

### What fights you — the enforcement half

The entire product thesis is **modifying** traffic (`bpf_probe_write_user` to kill a request). Pixie is architecturally an observer:

- Its probes are built around read-only capture + FD/connection correlation; every design decision optimizes for "trace everything safely without touching it."
- A kill means injecting `bpf_probe_write_user` (requires `CAP_SYS_ADMIN`, treated as dangerous) into BPF code that was never meant to write. Upstream Pixie would never accept that patch — so it lives on a **permanent private fork**.
- A permanent fork defeats the main reason to fork: you stop getting free upstream maintenance, because the critical feature can never merge back. You now maintain a heavily-diverged copy of a large C++ system indefinitely.

### Licensing

Pixie's BPF code is **GPL-2.0** (see the file headers); our agent is Apache / dual-BSD-GPL.

- **Forking Pixie** → the derivative is bound by GPL, which may matter to legal/customers for a commercial AI-liability product.
- **Copying snippets** into our repo carries the same derivative-work obligation. *Reading* their code to learn a technique is fine; *pasting* it is not. This is the specific constraint for Phase G.5 (BoringSSL/Go/Node): study the offset-table approach, reimplement clean-room — do not copy.

### Size mismatch

Pixie is not just the BPF tracer. Running it pulls in Stirling (C++ collector) + PEM + Vizier + the PxL query engine + the Pixie cloud/UI deployment model. Our agent is ~400 lines of BPF plus a focused Go binary that long-polls the existing backend. Forking Pixie to ship an LLM-kill daemon is buying a freight train to deliver one parcel.

### When extending Pixie *would* make sense

- If the roadmap pivots to **broad observability** ("inventory every AI tool, every protocol, across the cluster"), Pixie's capture engine becomes a major accelerant and our narrow agent becomes the limiting factor.
- For the **specific hard parts** (BoringSSL / Go `crypto/tls` / Node TLSWrap tracing), Pixie is the best reference implementation that exists — borrow the *techniques*, not the codebase.

### Recommendation

Keep the lean, enforcement-first agent; treat Pixie as a **reference, not a base**.

- For capture breadth we lack (multi-runtime TLS), study Pixie's approach and reimplement clean-room in our Apache codebase.
- Don't fork: GPL entanglement + permanent divergence + system bloat outweigh the head start, precisely because our differentiator (kill) is the one thing Pixie is built never to do.

> One-line version: we could fork Pixie, but our core feature — modifying traffic to kill a call — is something Pixie is built never to do and would never upstream, so we'd own a heavy GPL fork forever. Better to keep the focused agent and borrow Pixie's techniques for the multi-runtime capture we don't have yet.

## 9. Source map for reviewers

| Concern | File |
| --- | --- |
| Which symbols, attach logic | `apps/inferspect-agent/internal/kernel/loader.go` |
| BPF programs, maps, plaintext copy, enforcement | `apps/inferspect-agent/bpf/ssl_uprobe.c` |
| HTTP reassembly (user-space) | `apps/inferspect-agent/internal/http/reassembler.go` |
| Provider identification | `apps/inferspect-agent/internal/llm/identifier.go` |
| Ring buffer consumer | `apps/inferspect-agent/internal/ringbuf/reader.go` |
| Kernel↔userspace data-plane writeup | `ARCHITECTURE.md` (eBPF agent section) |
| Design rationale + kill flow | `docs/PLAN.md` (Phase G section) |
