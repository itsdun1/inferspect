# Repello AI (repello.ai) — deep dive

Comprehensive research on Repello: company, products, technical architecture, open-source vs from-scratch, and how it compares to our eBPF runtime-defense + AI-liability play. Compiled from their site, docs, blog/research, public GitHub repos + PyPI, job posts, founder podcasts, and funding press (June 2026). Same format as `straiker-deep-dive.md`.

> **Confidence note:** their performance/coverage numbers ("15M+ attack patterns", "<100ms", "270+ vuln types") are **self-reported marketing**. Architecture facts come from their docs, their own public OSS repos, and a CTO podcast — strong evidence; core engine internals (ARTEMIS/ARGUS) are closed and server-side.

---

## 1. Company snapshot

| | |
|---|---|
| **Tagline** | "Make AI safe to trust" — offensive-first, full-lifecycle AI security |
| **Founded** | 2024 (some DBs say 2023) |
| **HQ** | Dual: **San Francisco + Bengaluru** (Delaware legal entity) |
| **Funding** | **~$1.2M seed** (June 2025), led by **Venture Highway** (now part of General Catalyst); pi Ventures, **Entrepreneur First** (accelerator — not YC); angels incl. Charles Songhurst, Vivek Raghavan (Sarvam), Matt Clifford (EF), Neeraj Arora (ex-WhatsApp), Hari Srinivasan (Palo Alto) |
| **Founders** | **Aryaman Behera** (CEO, ex-Microsoft Azure Red Team, offensive-security) + **Naman Mishra** (CTO, ML-security research; SF-based). Both **IIT Roorkee**. |
| **Team** | Not disclosed (small; seed-stage). Founding ML Engineer: Lavish Bansal. |
| **Customers (named)** | **Groww**, **PhysicsWallah** (Indian unicorns); Lyzr (published red-team). Site logos (Azure, DocuSign, Thomson Reuters) likely pilots/design wins. |
| **Recognition** | Gartner "Emerging Tech: Top-Funded Startups in AI TRiSM — Agentic AI" report; Nvidia Inception; Microsoft for Startups |
| **Pricing** | Not public (contact sales) |

Vs Straiker: Repello is **much earlier and smaller** ($1.2M seed vs Straiker's $21M), India-origin/US-facing, red-teaming-first.

## 2. Products

A 3-phase pipeline (**discover → test → defend**) plus open-source dev tools:

- **Inventory** — AI asset discovery; "AI Bill of Materials" across apps/models/datasets/infra/MCPs; agent + threat graphs (attack paths, blast radius).
- **ARTEMIS** — **their core strength.** Automated/autonomous red-teaming: parallel attack threads, multi-stage exploit chains, multimodal (text/image/audio, 100+ languages), **Browser Mode** (human-like UI testing, not just API), CI/CD-integrated. Maps to OWASP LLM Top 10 / MITRE ATLAS / NIST / ISO 42001 / EU AI Act.
- **ARGUS** (a.k.a. Repello Guard) — runtime guardrails / "AI firewall." Six checks: prompt injection, toxicity/bias, brand/defamation, topic filtering, prompt-leakage (semantic overlap 0–1), policy enforcement. Multimodal, 100+ languages. **Calibrated from ARTEMIS findings** (the central architectural pitch → lower false positives).
- **MCP Gateway** — drop-in proxy for MCP visibility/control; per-tool authz, blocks unattested servers, audit trails; integrates with CrowdStrike/SentinelOne/Splunk/Jamf.
- **Workstation Lens** — security for coding agents on dev workstations (thinner/less-defined).
- **Open source:** `Agent-Wiz` (CLI, agent-workflow threat modeling via MAESTRO; 378★) and `whistleblower` (system-prompt extraction). Both orchestrate OpenAI.

## 3. Technical architecture — how it works

### Layer: **application layer, not kernel/network**
ARGUS ships as a **Python SDK** (`ArgusClient.scan_input`/`scan_output`) or an **HTTP API-proxy** ("drop it in front of your LLM call — or behind it"). MCP Gateway is an **MCP-protocol proxy**. There is **no eBPF, no libssl/TLS interception, no in-kernel kill, no syscall-level PII redaction** anywhere. PII handling is text inspection inside the SDK/API path.

### Enforcement: yes, but integration-required
ARGUS does inline synchronous blocking (claimed P99 <80ms, 3k req/s, 99.9% SLA) — **but only if the app routes through it** (SDK call or proxy re-route). It cannot touch traffic the app makes out-of-band. Actions: auto-block, soft-block, rewrite, or log.

### Detection mechanism
Multi-signal layered design (their LLM-guardrails guide): semantic-aware input analysis, behavioral/statistical anomaly on token distributions, structural injection-syntax checks, output PII/credential/system-prompt-leak detection, RAG content inspection, tool-call allow-listing. Whether detectors are fine-tuned models or LLM-judges is **undisclosed**; <80ms P99 implies small classifiers on the hot path.

### Red-team engine (ARTEMIS)
Profiles the target, generates tailored attacks, runs parallel adaptive attack threads, chains exploits. Browser Mode does multi-turn social-engineering chains via UI. Generation mechanism (LLM-adversary loop vs templated mutation) not fully disclosed; their public tools orchestrate frontier LLMs, so ARTEMIS is most likely a **custom orchestration layer driving frontier LLMs against a curated attack corpus**.

### Research (genuinely substantive)
First-party agentic-jailbreak benchmark (21 multi-turn scenarios in a sandbox with FS + exec tools; GPT-5.x, Claude Opus 4.5). Notable taxonomy contribution: their **"BREACHED" definition** — a scenario counts as breached if the model "materially advances an attacker's objective at any point... including intermediate steps, tool calls, and hidden side effects." Trajectory-level, multi-turn — worth borrowing conceptually. Published breach rates (e.g., 4.8% Claude vs 28.6% GPT-4-class). Third-party red-team of Lyzr.

## 4. Open source vs from-scratch — the verdict

| Layer | Build vs OSS | Confidence |
|---|---|---|
| **ARTEMIS SDK** (`repello-artemis-sdk` on PyPI) | Thin REST client, only dep `requests` — **engine is server-side/closed** | Confirmed |
| **ARTEMIS engine** | Closed. **Not** a wrapper of garak/PyRIT/promptfoo (they explicitly distance from "open-source scanners"). Custom orchestration + proprietary attack corpus. | Confirmed closed; custom = inference |
| **ARGUS runtime** | Closed; SDK/API **proxy** (not eBPF). No NeMo/LLM Guard/Llama Guard/Presidio mention. | Confirmed proxy; detector models undisclosed |
| **`Agent-Wiz`, `whistleblower`** (their public OSS) | Orchestrate **OpenAI API** + HuggingFace/Torch; standard ML stack | Confirmed |
| **Model layer** | **No from-scratch training.** Orchestrate frontier LLMs (OpenAI/Anthropic) for attack gen; possibly small fine-tuned classifiers for ARGUS (unconfirmed) | Inference |

**Headline:** Repello = "OSS + frontier-LLM APIs for plumbing, proprietary/closed for the differentiating engine." Their moat is the **attack corpus + red-team research + the discovery→test→runtime feedback loop**, not novel models or low-level systems work.

### Engineering stack (from job posts + CTO podcast)
- **Python-primary**; second language open-ended ("TypeScript/Node, Go, Rust, or similar") — **not** a systems/kernel shop.
- Vector DBs (Pinecone/Weaviate/Qdrant/pgvector/FAISS/Chroma), eval harnesses (DeepEval, OpenAI Evals), serving (vLLM/TensorRT-LLM/Ray Serve/KServe/SageMaker/Vertex — "nice to have").
- Cloud: GCP and/or AWS. **Zero mention of eBPF/kernel/XDP/dataplane anywhere** in hiring or talks.
- No patents (expected for a 2024 seed startup).
- CTO podcast ("From Prompt Injection to Reverse Shells") frames their worldview as **application/agent-layer**, not OS/kernel.

## 5. Repello vs us (Inferspect / Ollive)

| Dimension | Inferspect (us) | Repello |
|---|---|---|
| **Capture layer** | In-kernel **libssl** uprobe (transparent) | App-layer SDK / HTTP API-proxy / MCP proxy |
| **Integration cost** | **None — daemon-only, no app change** | Code change (SDK) or re-route traffic (proxy) |
| **Enforcement** | **In-kernel kill/modify, default for all egress** | Inline block in SDK/proxy — only if integrated |
| **Coverage** | All TLS egress, can't be bypassed | Misses anything not routed through SDK/proxy |
| **PII** | **On-host redaction below the app** | Text inspection in SDK/API path |
| **Conversation identity** | **Content-derived** (prefix + rolling hash) | SDK session state |
| **Red-teaming / pre-deploy** | none yet | **ARTEMIS — strong, their core** |
| **Runtime detection ML** | anchor/pattern + roadmap | layered classifiers (real, but younger product) |
| **Insurance / risk transfer** | **Core thesis** | **None** |

### Where Repello is ahead
- **Pre-deployment red-teaming (ARTEMIS)** — mature, multimodal, browser-mode, their verifiable strength.
- Multi-turn agentic breach taxonomy + first-party research.
- The discovery→test→runtime closed loop (calibrate guardrails from your own red-team findings).

### Our white space (Repello cannot claim)
1. **Transparent, daemon-only, in-kernel enforcement** — Repello requires SDK/proxy integration; no eBPF, no kernel competence in their hiring. Can't-be-bypassed capture is ours.
2. **On-host PII redaction** — data never leaves the host; Repello inspects text passing through its SDK/cloud.
3. **Content-derived conversation identity** — no app cooperation.
4. **AI-liability insurance / risk transfer** — none of these vendors (Repello, Straiker, Lakera, etc.) offer it. Open white space.

### Where we'll be pressed
Same as with Straiker: depth of semantic detection + the lack of a red-teaming product. An eBPF data-plane alone doesn't out-detect ARTEMIS/ARGUS on prompt-injection nuance. The strongest frame for us is **"runtime/infrastructure enforcement + governance evidence for underwriting"** — behavioral underwriting, which is exactly where the 2026 AI-insurance market is heading and where an eBPF audit trail is a natural fit.

## 6. Repello vs Straiker (since they're the two closest)

| | Repello | Straiker |
|---|---|---|
| Stage / funding | Seed, ~$1.2M (2025) | ~$21M (2025), $100k/yr ACV |
| Origin | India/US, IIT Roorkee founders | Sunnyvale, ex-Prisma Cloud / Akamai founders |
| Core strength | Red-teaming (ARTEMIS) | Detection model cloud (Defend) + breadth |
| Runtime latency disclosed | P99 <80ms (docs) | <130ms classic / <300ms agentic |
| Same thesis | discovery→test→runtime closed loop | discover→test→defend closed loop |
| Layer | app-layer SDK/proxy | app-layer gateway/SDK; eBPF observe-only sensor |
| Insurance | none | none |

They are near-identical in thesis (offense→runtime closed loop, app-layer, agentic/MCP focus). Straiker is better-funded with a deeper model cloud and an (observe-only) eBPF sensor; Repello is leaner and red-teaming-led. **Neither operates in-kernel and neither touches insurance** — the same two gaps that define our wedge.

## 7. Strategic takeaways

1. **Both Repello and Straiker validate the market and the "closed-loop" architecture** — but both are app-layer, integration-required, and detection-ML-led. We don't beat them at detection ML near-term; we beat them on **deployment model + enforcement layer + risk transfer**.
2. **Their plumbing is borrowable / standard** (frontier-LLM orchestration, OWASP/MITRE mappings, OSS ML stack). The IP is the attack corpus + research loop. We should likewise **borrow detection logic** (and conceptually their "BREACHED" trajectory-level judging) rather than rebuild.
3. **No competitor in this set offers insurance/risk-transfer.** Reinforced again — it's the clearest, most defensible wedge, and the 2026 market is shifting toward behavioral/governance-based AI underwriting that our eBPF evidence trail directly serves.
4. **Neither does in-kernel enforcement.** Our transparent capture-and-kill remains unmatched across both Straiker and Repello.

---

## Sources

Company/funding: repello.ai/about-us, /blog/big-news-repello-ai-raises-1-2m; indianstartupnews.com; outlookbusiness.com; india.entrepreneur.com; joinef.com/companies/repello-ai; crunchbase/tracxn.
Products/docs: repello.ai (home, /product, /argus, /inventory, /mcp-gateway); docs.repello.ai; blog (introducing-artemis, introducing-argus, llm-guardrails, artemis-browser-mode, mitre-atlas-framework, best-ai-security-tools).
Open source: github.com/Repello-AI (Agent-Wiz, whistleblower, repello-artemis-sdk, artemis-gh-action); pypi.org/project/repello-artemis-sdk, /repello-agent-wiz.
Stack/talks: repello.ai/careers/ai-engineer; Crazy Wisdom Podcast #448 (Naman Mishra); Pitch Cafe (Aryaman Behera).
Research: repello.ai/blog (agentic jailbreak benchmark, "BREACHED" taxonomy); lyzr.ai/blog/validating-lyzr-ai-security-with-repello.
Market context: insurancejournal.com (AI insurance ≠ cyber), riskandinsurance.com (AI liability claims surge).
