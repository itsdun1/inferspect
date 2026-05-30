# eBPF tooling landscape — what else could help us

Companion to `pixie-comparison.md`. That doc compares us to Pixie in depth; this one surveys the *other* open-source eBPF tools that could accelerate either half of our agent — **capture** (read LLM traffic) or **enforcement** (act on it) — and says where each fits.

What we need, restated:

1. **Capture** — read plaintext LLM API traffic via uprobes on TLS libraries. Today: OpenSSL only. Gap: Go `crypto/tls`, Node, BoringSSL (Phase G.5).
2. **Enforcement** — modify/kill a specific request in-flight. This is rare; most tools only observe.
3. **Constraints** — lightweight daemon, permissive license (we're Apache / dual-BSD-GPL), single-host today.

> Licenses below were checked against project pages/search May 2026; confirm against the repo `LICENSE` before relying on any for legal decisions.

---

## The landscape at a glance

| Tool | Category | License | TLS capture | Enforcement (modify/block) | Relevance to us |
| --- | --- | --- | --- | --- | --- |
| **Pixie** | Observability | Apache-2.0 (BPF is GPL-2.0) | OpenSSL, BoringSSL/Go, Node | ❌ none | capture reference (see other doc) |
| **eCapture** | TLS capture tool | **MIT** | OpenSSL 1.0.2–3.5, BoringSSL, GnuTLS, NSS/NSPR, **GoTLS** | ❌ none | ⭐ best capture reference; MIT = reusable |
| **Grafana Beyla / OBI** | Auto-instrumentation | Apache-2.0 | OpenSSL + Go | ❌ none | OTel export reference; multi-language |
| **Tetragon** | Security + enforcement | Apache-2.0 (BPF is GPL-2.0) | not its focus (syscall/process layer) | ✅ SIGKILL + override-return | ⭐ enforcement reference |
| **KubeArmor** | Runtime enforcement | Apache-2.0 | no (file/proc/net layer) | ✅ BPF-LSM allow/deny | enforcement model reference |
| **Falco** | Detection | Apache-2.0 | no (syscall events) | ⚠️ alert; response via Talon | detection-rules reference |
| **Tracee** | Security forensics | Apache-2.0 | no | ⚠️ detection-led | forensics reference |
| **DeepFlow / Coroot / Odigos / Kubeshark** | Observability | Apache/SSPL varies | OpenSSL/Go (varies) | ❌ none | minor; capture-only |

Two stand out for us: **eCapture** for the capture half, **Tetragon** for the enforcement half.

---

## Capture-focused tools (observe only)

### eCapture — the closest thing to our capture half ⭐

`github.com/gojue/ecapture` — a purpose-built tool that captures SSL/TLS plaintext via eBPF uprobes, no CA cert, no TLS termination. Exactly the technique we use, but far broader and more mature on the capture side:

- **Libraries:** OpenSSL 1.0.2 → 3.5.x, BoringSSL (incl. Android), GnuTLS, NSS/NSPR, **and GoTLS** — i.e. it already covers the Go/Node/BoringSSL surface that is our Phase G.5 gap.
- **Platforms:** Linux + Android, amd64 + arm64.
- **Output:** text, PCAP-NG, or `SSLKEYLOGFILE` keylog format.
- **License: MIT** — crucially *not* GPL. We could reference, adapt, or even reuse code (with attribution) without the copyleft entanglement Pixie carries.

**Why it matters:** for the capture half — especially the multi-runtime TLS coverage we lack — eCapture is a better, lighter, more license-friendly reference than Pixie. It's single-purpose (just capture), so there's no Stirling/Vizier stack to inherit. It has **no enforcement**, so it doesn't replace our kill path; it informs the capture path.

### Grafana Beyla / OBI

`grafana/beyla` — eBPF auto-instrumentation that emits OpenTelemetry metrics + traces for HTTP/gRPC, with TLS tracing via OpenSSL + Go uprobes. Apache-2.0. Now donated to CNCF as **OpenTelemetry eBPF Instrumentation (OBI)**.

Relevance: a clean reference for *how to export* what we capture as OTel (relevant to the earlier "can we use OpenTelemetry instead of HTTP" question), and for multi-language uprobe placement. Observability only — no enforcement.

### Others (capture-only, lower relevance)

DeepFlow, Coroot, Odigos, Kubeshark — all eBPF observability with some SSL-uprobe support. None enforce. Useful only as additional capture references; eCapture + Beyla cover the same ground better for our needs.

---

## Enforcement-capable tools (the rare + valuable group)

Almost no eBPF tool *modifies* traffic. The few that *act* do so at the **process/syscall/LSM** layer, not by rewriting a specific request's bytes. Worth understanding because they prove production enforcement patterns and offer alternatives to our buffer-corruption kill.

### Tetragon — eBPF enforcement, proven in production ⭐

`cilium/tetragon` (Isovalent/Cisco, Apache-2.0; BPF parts GPL-2.0). Security observability **and runtime enforcement**. Two enforcement mechanisms, both directly relevant:

1. **`SIGKILL` / signal** — terminate the offending process immediately when a policy matches.
2. **Override return value** — make a hooked function/syscall return an error to its caller, using the kernel error-injection framework (needs `CONFIG_BPF_KPROBE_OVERRIDE`).

**Why it matters:** Tetragon's *override-return* is an alternative to how we kill. Today we corrupt the request buffer (`bpf_probe_write_user`) so the server rejects it. We could instead **override `SSL_write` to return an error** — the app sees a clean write failure directly, no malformed bytes on the wire. Tetragon proves this mechanism works at scale. (Its `SIGKILL` path is too blunt for us — it kills the whole app process, not one conversation.)

### KubeArmor — inline enforcement via BPF-LSM

`kubearmor/KubeArmor` (CNCF sandbox, Apache-2.0). Enforces allow/deny on files, processes, and network using **BPF-LSM** (and AppArmor/SELinux). When an LSM hook fires, the eBPF program looks up policy in BPF maps and returns allow (`0`) or block (`-EPERM`) — decided entirely in-kernel, no user-space round-trip.

**Why it matters:** a different enforcement model — *policy-as-LSM-hook* rather than *operator-triggered buffer edit*. Not a fit for "this HTTP body is bad" (there's no LSM hook for request content), but the BPF-map-driven in-kernel allow/deny pattern is a clean design reference if we ever add per-process or per-socket policy that doesn't need content inspection.

### Falco / Tracee — detection, not inline enforcement

Both are eBPF runtime-security tools focused on *detecting* and alerting on suspicious syscall activity (Falco is CNCF-graduated; Tracee is Aqua's). Enforcement is bolt-on and reactive (Falco Talon responds *after* an event). Useful as references for **rule/detector design** if we build the auto-kill intervention engine (see `agents-of-chaos.md`), not as a capture or inline-kill base.

---

## Enforcement primitives — the menu

Independent of any product, these are the eBPF kernel mechanisms for *acting* on traffic. Knowing the menu helps us pick the cleanest kill.

| Primitive | What it does | Fit for "kill one LLM request" |
| --- | --- | --- |
| `bpf_probe_write_user` | overwrite bytes in the app's buffer | ✅ what we do — corrupt the JSON so server returns 400 |
| `bpf_override_return` | force a function/syscall to return an error | ✅ cleaner alternative — make `SSL_write` fail directly (Tetragon uses this; needs `CONFIG_BPF_KPROBE_OVERRIDE`) |
| `bpf_send_signal` | send a signal (e.g. SIGKILL) to the process | ❌ too blunt — kills the whole app, not one conversation |
| BPF-LSM deny | allow/deny at a Linux Security Module hook | ❌ no LSM hook for "this request body is bad" |
| TC / XDP drop | drop/redirect packets at the network layer | ❌ traffic is encrypted by then; dropping is a blunt connection break |

Takeaway: our two realistic options are **buffer corruption** (current) and **override-return** (Tetragon-style). Override-return is arguably cleaner — the application gets an honest write error instead of a confusing server-side 400 — and is worth prototyping as a refinement. It carries a kernel-config dependency (`CONFIG_BPF_KPROBE_OVERRIDE`) that buffer corruption does not, so we'd keep corruption as the portable fallback.

---

## Building-block libraries (if we keep building our own)

Not tools — the frameworks you write an agent *with*:

| Library | Language | Notes |
| --- | --- | --- |
| **cilium/ebpf** | Go | what we use today — pure Go, no libbpf/CGo dependency |
| libbpf | C | the upstream standard; CO-RE; what most C agents use |
| libbpfgo | Go (CGo over libbpf) | Tracee uses it; heavier than cilium/ebpf but full libbpf features |
| BCC | Python/C | what Pixie uses; runtime compilation, heavier footprint |
| Aya | Rust | pure-Rust eBPF; growing ecosystem |

We're on `cilium/ebpf`, which is the right choice for a lean Go agent — no CGo, no runtime clang, static binary.

---

## Recommendations for our case

1. **Capture half → study (and possibly reuse) eCapture, not Pixie.** It's MIT (no copyleft), purpose-built for exactly our task, and already covers the Go/BoringSSL/Node TLS surface that is our Phase G.5 gap. It's the single highest-leverage reference in this list.
2. **Enforcement half → study Tetragon's override-return.** It's a cleaner kill than buffer corruption (app sees an honest error), proven in production. Prototype it as a refinement; keep `bpf_probe_write_user` as the portable fallback for kernels without `CONFIG_BPF_KPROBE_OVERRIDE`.
3. **Detector design (future auto-kill) → look at Falco/Tracee rule models**, mapped onto the Agents-of-Chaos failure taxonomy.
4. **Stay on `cilium/ebpf`** for the agent itself — lightest fit for a Go daemon.
5. **No single tool does both halves the way we need.** The ones that capture broadly (eCapture, Beyla, Pixie) don't enforce; the ones that enforce (Tetragon, KubeArmor) don't capture LLM request content. Our differentiator — capture LLM HTTPS *and* modify it per-conversation — remains something we assemble, borrowing the capture technique from eCapture and the enforcement technique from Tetragon.

---

## Sources

- eCapture — <https://github.com/gojue/ecapture>, <https://ecapture.cc/>
- Tetragon enforcement — <https://tetragon.io/docs/concepts/enforcement/>
- KubeArmor — <https://kubearmor.io/>, <https://docs.kubearmor.io/kubearmor/quick-links/kubearmor_overview/runtime_enforcer>
- Grafana Beyla / OBI — <https://grafana.com/oss/beyla-ebpf/>, <https://github.com/grafana/beyla>
- Pixie eBPF TLS tracing background — <https://blog.px.dev/ebpf-tls-tracing-past-present-future/>
