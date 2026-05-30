# Ollive — Documentation Index

The documentation hierarchy for the Ollive platform. Start at the README, drop into ARCHITECTURE for the data plane, read PLAN for the design rationale.

## Top-level

- [README](../README.md) — product positioning, quick start, setup, what each component is and why we picked it, schema design, tradeoffs, roadmap, repo layout, testing, deployment.
- [ARCHITECTURE](../ARCHITECTURE.md) — deep dive: ingestion flow, SDK design, LangGraph integration, failure handling, idempotency layering, scaling envelopes, security posture.

## In this folder

- [PLAN](./PLAN.md) — the design document: what was built and why, tech stack rationale, log schemas, controller/service/repository pattern, service layout, deployment phases.

## Research

- [research/agents-of-chaos.md](./research/agents-of-chaos.md) — primer on the four-layer LLM agent attack taxonomy (input / tool / memory / output). Backs the Phase G runtime-defense premise: why traditional prompt-injection benchmarks miss 30–60% of production failure modes and how Ollive's eBPF agent maps onto each layer.
- [research/pixie-comparison.md](./research/pixie-comparison.md) — eBPF TLS-tracing comparison between our agent and Pixie (CNCF). Extracts our libssl-probing code, verifies Pixie's approach against its source, and lays out where we match (uprobe-on-library, read-before-encrypt) vs differ (in-kernel enforcement, ring buffer, on-host PII, narrow scope).
- [research/ebpf-tooling-landscape.md](./research/ebpf-tooling-landscape.md) — survey of other open-source eBPF tools that could accelerate capture (eCapture ⭐ MIT, Grafana Beyla/OBI) or enforcement (Tetragon ⭐, KubeArmor), the eBPF enforcement-primitive menu (`bpf_probe_write_user` vs `bpf_override_return` vs `bpf_send_signal`), and build-it-with libraries. Recommends eCapture as the capture reference and Tetragon's override-return as a cleaner kill to prototype.

## Historical reference

- [`../chatbot.txt`](../chatbot.txt) — the original brief that prompted the initial design. Kept in the repo for historical reference; not load-bearing for current development.
