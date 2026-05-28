// SPDX-License-Identifier: Apache-2.0
//
// Inferspect agent — libssl uprobes.
//
// Two uprobes (entry of SSL_write, return of SSL_read) push captured plaintext
// to a ringbuf consumed by user-space. The agent reassembles HTTP/JSON in
// user-space because eBPF can't safely parse arbitrary JSON.
//
// Soft-block enforcement: the user-space agent populates the ``blocked_sockets``
// LRU hash with ``(pid, fd)`` tuples that should be killed. On SSL_write entry
// the program checks the map; on hit, it zeros out the buffer length argument
// (clobbering the user-space SSL state) and uses bpf_override_return to make
// SSL_write report 0 bytes written. The customer's app sees a TLS write error.
//
// This file is built with:
//     clang -O2 -g -target bpf -D__TARGET_ARCH_x86 -c bpf/ssl_uprobe.c -o ssl_uprobe.o
//
// The loader (internal/kernel/loader.go) attaches the programs to libssl's
// SSL_write/SSL_read symbols on the host. Both OpenSSL 1.1 and 3.x are
// supported because the ABI shape (ssl, buf, num) hasn't changed.

/* Pick the right minimal vmlinux shim for the BPF target arch. The build
 * passes -D__TARGET_ARCH_x86 or -D__TARGET_ARCH_arm64 (matching the host
 * kernel) so libbpf's tracing macros emit the correct register references. */
#if defined(__TARGET_ARCH_arm64)
#include "vmlinux_aarch64.h"
#else
#include "vmlinux_x86_64.h"
#endif
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

char LICENSE[] SEC("license") = "Dual BSD/GPL";

#define MAX_PAYLOAD 8192
#define MAX_EVENTS  4096

// Phase G.4 — in-kernel content-anchor matching. ANCHOR_MAX is the max byte
// length of a single anchor pattern. MAX_ANCHORS is the simultaneous-arm
// cap (kept small so total map size stays ~4 KiB and the verifier-bounded
// scan loop stays sane). SCAN_WINDOW is the prefix of the SSL_write buffer
// we scan; must be a power of 2 so the verifier accepts the masked length.
//
// Tuned for kernel 5.15 BPF verifier complexity ceiling (~4K jumps per
// program). Original 16/1024 combination produced an SSL_write_ex program
// the verifier rejected with "8193 jumps too complex." Halving both knobs
// drops the combinatorial cost while keeping coverage adequate for chat-
// completions request bodies (SCAN_WINDOW=512 catches up to ~4 KB of body
// once the ~200B HTTP headers are skipped).
#define ANCHOR_MAX     128
#define MAX_ANCHORS    8
#define SCAN_WINDOW    512
// FIRST_SEG is the cheap-filter prefix used in the inner unrolled compare:
// 16 bytes is enough to filter out random buffer content (chance of false
// positive on a random 16-byte window is ~1 in 2^128 for plain text) while
// keeping the unrolled compare loop tight enough for the verifier.
#define FIRST_SEG      16

// Event types we ship to user-space.
#define EVT_SSL_WRITE 1
#define EVT_SSL_READ  2
// EVT_SSL_KILL is emitted (instead of a normal capture event) when the
// in-kernel block fired on a known-bad SSL ctx. Carries no payload — just
// (ssl_ctx, pid) for accounting / audit.
#define EVT_SSL_KILL  3

/* Layout-stable header: 8-byte fields first to avoid C compiler padding.
 * The Go decoder mirrors this byte-for-byte. Keep field order in sync. */
struct ssl_event {
    __u64 ts_ns;
    __u64 ssl_ctx;         // SSL* pointer — identifies the connection
    __u32 pid;
    __u32 tid;
    __u32 len;             // bytes captured (≤ MAX_PAYLOAD)
    __u32 total;           // original len from the userspace call
    __u8  type;            // EVT_SSL_WRITE | EVT_SSL_READ
    __u8  truncated;       // 1 if payload didn't fit in MAX_PAYLOAD
    __u16 _pad;            // pad to 36 bytes — payload aligns at 36
    __u8  payload[MAX_PAYLOAD];
};

// Ringbuf: kernel → user stream.
struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 24);  // 16 MiB
} events SEC(".maps");

// User-space populates these per (pid, fd) to instruct the kernel to corrupt
// the next SSL_write on that connection. Value is unused (presence is the bit).
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __type(key, __u64);            // (pid << 32) | fd  — packed
    __type(value, __u8);
    __uint(max_entries, 8192);
} blocked_sockets SEC(".maps");

// blocked_ssl_contexts: SSL* pointers we've decided to disrupt.
// User-space writes here from cmd/agent/main.go after a fingerprint match.
// The SSL_write/SSL_write_ex entry probes look up the SSL ctx; on hit they
// corrupt the first byte of the request body via bpf_probe_write_user so the
// server (OpenAI / Anthropic) sees malformed JSON and returns HTTP 400.
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __type(key, __u64);            // SSL* cast to __u64
    __type(value, __u8);           // 1 = blocked
    __uint(max_entries, 8192);
} blocked_ssl_contexts SEC(".maps");

// blocked_pids: short-TTL PID-wide block. When user-space receives a kill
// command, it adds the offending PID here briefly (default 200ms). Catches
// the race where the customer's TLS connection pool opens a NEW connection
// for the next turn — a freshly-created SSL_ctx isn't in blocked_ssl_contexts
// yet, but the PID is, so the very first write on the new socket is
// disrupted. User-space removes entries after the TTL expires; unrelated
// concurrent traffic from the same PID gets caught during the window
// (acceptable collateral; agent un-arms unrelated SSL_ctxs after observing).
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __type(key, __u32);            // PID
    __type(value, __u8);           // 1 = blocked
    __uint(max_entries, 1024);
} blocked_pids SEC(".maps");

// blocked_anchors: Phase G.4 content-anchor table. Each slot holds a byte
// pattern (the "anchor") the kernel scans the outgoing SSL_write buffer for.
// User-space sets and clears slots as operators issue block_anchor /
// unblock_anchor commands. ``len == 0`` means the slot is empty.
//
// We use an ARRAY (not HASH) so the slot index is stable across
// arm/disarm — user-space-side accounting joins a kill back to the
// originating command via this index.
struct anchor_entry {
    __u32 len;                     // 0 = empty; otherwise bytes used
    __u8  bytes[ANCHOR_MAX];
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __type(key, __u32);
    __type(value, struct anchor_entry);
    __uint(max_entries, MAX_ANCHORS);
} blocked_anchors SEC(".maps");

// scan_scratch: per-CPU scratch page used to stage a copy of the outgoing
// SSL_write buffer for content-anchor matching. Per-CPU avoids any
// contention; the BPF verifier requires the buffer to live in a map (we
// can't stack-allocate 1 KiB on a BPF program).
struct scan_buf {
    __u8 buf[SCAN_WINDOW];
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key, __u32);
    __type(value, struct scan_buf);
    __uint(max_entries, 1);
} scan_scratch SEC(".maps");

// SSL_read needs a per-task stash of the (ssl, buf) pair so the return probe
// can read out the bytes actually placed into ``buf``.
struct ssl_read_args {
    __u64 ssl_ctx;
    __u64 buf;
    __u64 num;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u64);            // pid_tgid
    __type(value, struct ssl_read_args);
    __uint(max_entries, 4096);
} ssl_read_args SEC(".maps");

static __always_inline void emit_event(__u8 type, __u64 ssl_ctx, const void *buf, __u64 num) {
    struct ssl_event *ev;
    ev = bpf_ringbuf_reserve(&events, sizeof(*ev), 0);
    if (!ev) {
        return;
    }
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    ev->type = type;
    ev->_pad = 0;
    ev->pid = pid_tgid >> 32;
    ev->tid = pid_tgid & 0xFFFFFFFF;
    ev->ts_ns = bpf_ktime_get_ns();
    ev->ssl_ctx = ssl_ctx;
    ev->total = (__u32)num;

    /* The BPF verifier rejects variable-size copies unless it can prove the
     * size is non-negative and bounded. Masking with (MAX_PAYLOAD - 1) — a
     * power-of-two constant — gives it both bounds in a single op.
     *
     * Trade-off: we lose the edge case where num is an exact multiple of
     * MAX_PAYLOAD (cap collapses to 0). In practice LLM request bodies
     * never hit that alignment; ev->total still records the true size. */
    __u32 cap = ((__u32)num) & (MAX_PAYLOAD - 1);
    ev->len = cap;
    ev->truncated = num >= MAX_PAYLOAD ? 1 : 0;

    if (cap > 0 && buf != 0) {
        long r = bpf_probe_read_user(&ev->payload, cap, buf);
        if (r < 0) {
            ev->len = 0;
        }
    } else {
        ev->len = 0;
    }
    bpf_ringbuf_submit(ev, 0);
}

// buffer_matches_any_anchor scans the supplied scratch buffer for any
// armed anchor's byte pattern. Returns the matching slot index on hit, or
// -1 on miss.
//
// Strategy (verifier-friendly):
//   - Outer loop: position p in [0, SCAN_WINDOW - FIRST_SEG]. Compares the
//     anchor's first 32 bytes against scratch[p..p+32] for each anchor in
//     a single pass. This is the hot path; we keep it tight and bounded.
//   - On a first-segment hit we fall through to a full anchor-length
//     compare (anchor.len up to ANCHOR_MAX). Real first-segment hits are
//     rare (32-byte random-ish JSON tails ≈ collision-free), so the full
//     compare runs at most a handful of times per write.
//
// The outer loop iterates at most (SCAN_WINDOW - FIRST_SEG) = 992
// positions. With FIRST_SEG=32 unrolled and MAX_ANCHORS=16, the verifier
// sees ~500K total instructions if naively unrolled — too many. We avoid
// that by NOT unrolling the outer loop; clang emits a real backward branch
// that the verifier handles via bounded-loop detection (LLVM >= 10, kernel
// >= 5.3 supports this without explicit bpf_loop()).
static __always_inline int buffer_matches_any_anchor(const __u8 *scratch, __u32 readlen) {
    if (readlen < FIRST_SEG) {
        return -1;
    }
    __u32 max_start = readlen - FIRST_SEG;
    if (max_start > SCAN_WINDOW - FIRST_SEG) {
        max_start = SCAN_WINDOW - FIRST_SEG;
    }

    // Branchless XOR-accumulate per position. Each inner-loop body has no
    // `if`/`break`, so the BPF verifier's complexity ceiling (kernel 5.15
    // caps ~8K explored branches) isn't hit. We trade short-circuit speed
    // for verifier compatibility — at FIRST_SEG=16 and SCAN_WINDOW=512
    // we do 16 XOR+OR ops per position, ~8K ops per scan slot. Cheap.
    //
    // No tail-of-anchor check: FIRST_SEG bytes of the user-message text
    // is unique enough in practice (a 16-byte natural-text window has
    // ~2^120 effective entropy against random buffer content). If we want
    // exact-length match later, upgrade to bpf_loop (kernel ≥ 5.17).
    #pragma unroll
    for (__u32 slot = 0; slot < MAX_ANCHORS; slot++) {
        __u32 key = slot;
        struct anchor_entry *a = bpf_map_lookup_elem(&blocked_anchors, &key);
        if (!a) {
            continue;
        }
        if (a->len < FIRST_SEG) {
            continue;
        }

        for (__u32 p = 0; p <= max_start && p < (SCAN_WINDOW - FIRST_SEG); p++) {
            __u8 diff = 0;
            #pragma unroll
            for (__u32 i = 0; i < FIRST_SEG; i++) {
                diff |= scratch[(p + i) & (SCAN_WINDOW - 1)] ^ a->bytes[i];
            }
            if (diff == 0) {
                return (int)slot;
            }
        }
    }
    return -1;
}

// scan_buffer_for_anchor loads up to SCAN_WINDOW bytes from the user-space
// SSL_write buffer into the per-CPU scratch page, then walks the armed
// anchor table. Returns the matching slot index, or -1 on miss / failure.
static __always_inline int scan_buffer_for_anchor(const void *buf, __u64 num) {
    if (buf == 0 || num == 0) {
        return -1;
    }
    __u32 zero = 0;
    struct scan_buf *sb = bpf_map_lookup_elem(&scan_scratch, &zero);
    if (!sb) {
        return -1;
    }
    // Always read a fixed SCAN_WINDOW bytes. If the user buffer is
    // shorter, bpf_probe_read_user may return < 0 (partial copy from a
    // short page); we treat that as "scan as much as we got" by retrying
    // with a smaller fixed size. Avoids the verifier dance around a
    // variable readlen.
    __u32 readlen = SCAN_WINDOW;
    if (bpf_probe_read_user(sb->buf, SCAN_WINDOW, buf) < 0) {
        // Shorter buffer; try a tiny tail read so we at least scan the
        // start of the JSON body. 128 bytes covers a typical chat body's
        // {"messages":[{"role":"system","content":"..." prefix.
        readlen = 128;
        if (bpf_probe_read_user(sb->buf, 128, buf) < 0) {
            return -1;
        }
    }
    // Constrain readlen for the inner scan loop's verifier.
    readlen &= SCAN_WINDOW - 1;
    if (readlen == 0) {
        readlen = SCAN_WINDOW - 1;
    }
    return buffer_matches_any_anchor(sb->buf, readlen);
}

// enforce_if_blocked checks both the per-SSL_ctx and the per-PID block
// maps; on hit, corrupts the first byte of buf so the JSON body is malformed,
// then emits an EVT_SSL_KILL event for user-space accounting. Returns 1 if a
// kill was applied, 0 otherwise.
//
// Order of checks (cheapest first):
//   1. blocked_ssl_contexts hash lookup — O(1).
//   2. blocked_pids hash lookup — O(1).
//   3. blocked_anchors content scan — O(SCAN_WINDOW * MAX_ANCHORS) worst
//      case, only reached when no SSL_ctx / PID hit. Phase G.4.
static __always_inline int enforce_if_blocked(__u64 ssl_ctx, const void *buf, __u64 num) {
    int matched_slot = -1;
    __u8 *blocked = bpf_map_lookup_elem(&blocked_ssl_contexts, &ssl_ctx);
    if (blocked && *blocked != 0) {
        goto kill;
    }
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    __u8 *pid_blocked = bpf_map_lookup_elem(&blocked_pids, &pid);
    if (pid_blocked && *pid_blocked != 0) {
        goto kill;
    }
    // Phase G.4 — content-anchor scan. Only runs when neither SSL_ctx nor
    // PID is armed; on the cold path with no anchors armed, this returns
    // immediately after the first slot-lookup miss.
    matched_slot = scan_buffer_for_anchor(buf, num);
    if (matched_slot < 0) {
        return 0;
    }

kill:
    if (buf != 0 && num > 0) {
        /* Corrupt first byte. We can't reliably get an HTTP-aware position
         * because the buffer may contain headers + body or just body. The
         * first byte is enough — JSON parser at the server sees \0 prefix
         * and rejects with HTTP 400. */
        __u8 zero = 0;
        bpf_probe_write_user((void *)buf, &zero, 1);
    }
    /* Emit a marker event so the agent can mark enforcement_events.matched=1. */
    struct ssl_event *ev = bpf_ringbuf_reserve(&events, sizeof(*ev), 0);
    if (ev) {
        __u64 pid_tgid2 = bpf_get_current_pid_tgid();
        ev->ts_ns = bpf_ktime_get_ns();
        ev->ssl_ctx = ssl_ctx;
        ev->pid = pid_tgid2 >> 32;
        ev->tid = pid_tgid2 & 0xFFFFFFFF;
        ev->len = 0;
        ev->total = (__u32)num;
        ev->type = EVT_SSL_KILL;
        ev->truncated = 0;
        // Stash the matching anchor slot in _pad so user-space can join
        // back to the originating command id. -1 → no anchor (SSL_ctx or
        // PID hit). We bias by +1 so a 0 slot is distinguishable from
        // "no anchor".
        ev->_pad = (__u16)(matched_slot + 1);
        bpf_ringbuf_submit(ev, 0);
    }
    return 1;
}

// SSL_write(SSL *ssl, const void *buf, int num)
SEC("uprobe/SSL_write")
int BPF_KPROBE(uprobe_SSL_write, void *ssl, const void *buf, int num) {
    if (num <= 0) {
        return 0;
    }
    if (enforce_if_blocked((__u64)ssl, buf, (__u64)num)) {
        return 0;
    }
    emit_event(EVT_SSL_WRITE, (__u64)ssl, buf, (__u64)num);
    return 0;
}

// SSL_write_ex(SSL *ssl, const void *buf, size_t num, size_t *written)
// OpenSSL 3 prefers this — Python's _ssl module + httpx + curl all call _ex.
// Same first three args; we emit the same EVT_SSL_WRITE event at entry.
SEC("uprobe/SSL_write_ex")
int BPF_KPROBE(uprobe_SSL_write_ex, void *ssl, const void *buf, __u64 num) {
    if (num == 0) {
        return 0;
    }
    if (enforce_if_blocked((__u64)ssl, buf, num)) {
        return 0;
    }
    emit_event(EVT_SSL_WRITE, (__u64)ssl, buf, num);
    return 0;
}

// SSL_read(SSL *ssl, void *buf, int num) — stash args, read on return.
SEC("uprobe/SSL_read")
int BPF_KPROBE(uprobe_SSL_read_entry, void *ssl, void *buf, int num) {
    __u64 id = bpf_get_current_pid_tgid();
    struct ssl_read_args a = {
        .ssl_ctx = (__u64)ssl,
        .buf = (__u64)buf,
        .num = (__u64)num,
    };
    bpf_map_update_elem(&ssl_read_args, &id, &a, BPF_ANY);
    return 0;
}

SEC("uretprobe/SSL_read")
int BPF_KRETPROBE(uretprobe_SSL_read, int ret) {
    __u64 id = bpf_get_current_pid_tgid();
    struct ssl_read_args *a = bpf_map_lookup_elem(&ssl_read_args, &id);
    if (!a) {
        return 0;
    }
    if (ret > 0) {
        emit_event(EVT_SSL_READ, a->ssl_ctx, (void *)a->buf, (__u64)ret);
    }
    bpf_map_delete_elem(&ssl_read_args, &id);
    return 0;
}

// SSL_read_ex(SSL *ssl, void *buf, size_t num, size_t *readbytes)
// Stash the buffer + out-param pointer; on return read the actual bytes-read
// from *readbytes if the call succeeded.
struct ssl_read_ex_args {
    __u64 ssl_ctx;
    __u64 buf;
    __u64 num;
    __u64 readbytes_ptr;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u64);
    __type(value, struct ssl_read_ex_args);
    __uint(max_entries, 4096);
} ssl_read_ex_args SEC(".maps");

SEC("uprobe/SSL_read_ex")
int BPF_KPROBE(uprobe_SSL_read_ex_entry, void *ssl, void *buf, __u64 num, __u64 *readbytes) {
    __u64 id = bpf_get_current_pid_tgid();
    struct ssl_read_ex_args a = {
        .ssl_ctx = (__u64)ssl,
        .buf = (__u64)buf,
        .num = num,
        .readbytes_ptr = (__u64)readbytes,
    };
    bpf_map_update_elem(&ssl_read_ex_args, &id, &a, BPF_ANY);
    return 0;
}

SEC("uretprobe/SSL_read_ex")
int BPF_KRETPROBE(uretprobe_SSL_read_ex, int ret) {
    __u64 id = bpf_get_current_pid_tgid();
    struct ssl_read_ex_args *a = bpf_map_lookup_elem(&ssl_read_ex_args, &id);
    if (!a) {
        return 0;
    }
    if (ret > 0 && a->readbytes_ptr != 0) {
        __u64 readbytes = 0;
        if (bpf_probe_read_user(&readbytes, sizeof(readbytes), (void *)a->readbytes_ptr) == 0 &&
            readbytes > 0) {
            emit_event(EVT_SSL_READ, a->ssl_ctx, (void *)a->buf, readbytes);
        }
    }
    bpf_map_delete_elem(&ssl_read_ex_args, &id);
    return 0;
}
