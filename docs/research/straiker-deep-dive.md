# Straiker (straiker.ai) — deep dive

Comprehensive research on Straiker: company, products, technical architecture, open-source vs from-scratch, and how it compares to our eBPF runtime-defense + AI-liability play. Compiled from their site, product pages, blog/STAR research, public integration repos, job posts, patents, and third-party coverage (June 2026).

> **Confidence note:** facts below are tagged where it matters. Their performance numbers (98.1% accuracy, <300ms, 6–21× FP) are **self-reported and unaudited**. Architecture details come from their own pages plus their employees' public integration repos and the CTO's prior patents — strong evidence, but core model IP is not publicly disclosed.

---

## 1. Company snapshot

| | |
|---|---|
| **Tagline** | "The Agentic AI Security Company" / "agentic-first AI security" |
| **Founded** | 2023 (stealth); launched March 27, 2025 |
| **HQ** | Sunnyvale, CA |
| **Funding** | $21M (single disclosed round), led by **Lightspeed** + **Bain Capital Ventures** (Rain Capital also in). Valuation undisclosed. No Series B announced. |
| **CEO** | **Ankur Shah** — ex-SVP/GM **Prisma Cloud at Palo Alto Networks** (came via RedLock acquisition); ex-CipherCloud, Symantec |
| **CTO** | **Sreenath Kurupati** — ex-VP AI/Data Science/Security Research at **Akamai**; ex-founder/CEO Cyberfend (acq. by Akamai); 15+ yrs Intel (computer vision) |
| **Team** | ~40 (grew 15→40 in <12 months) |
| **Traction** | "8× growth in 6 months"; six- and seven-figure deals; CB Insights AI 100 (May 2026) |
| **Customers (named)** | DirecTV, Comcast, Fortinet, Coupang, Greenlight, Automation Anywhere, Deloitte, Omada Health, Coupa, Amex GBT, EnterpriseDB, Workato, People.ai |
| **Pricing** | ~$100k/yr (AWS Marketplace), enterprise sales motion, no self-serve tier |

## 2. Products — three-product lifecycle (discover → test → defend)

**Discover AI** — visibility & posture. Auto-inventory of every agent, tool, MCP server across cloud/SaaS/on-prem; AI-SPM / Agent-SPM; MCP vulnerability DB (12,000+); maps to OWASP Agentic Top 10, MITRE ATLAS, NIST AI RMF, EU AI Act. (Newest module, added 2026.)

**Ascend AI** — continuous red-teaming. Autonomous offensive agents (recon → STRIDE threat-model → scenario → exploit) that test the way attackers do, continuous/scheduled/on-demand, across dev/staging/**prod**. CI/CD-native. Coverage: prompt injection, MCP tool misuse, data/PII/PCI/HIPAA leakage. Proprietary terms: **LAVA** (Language-Augmented Vulnerability in Applications), **typoglycemia** evasion, **STAR** framework.

**Defend AI** — runtime detection + blocking. "Industry-first runtime engine trained on millions of real-world agent traces." Detects/blocks direct + indirect prompt injection, data exfil, tool manipulation; multimodal (text/code/image/audio/files); full-chain trace analysis (user→model→tool→agent-to-agent); instruction-drift detection over turns. Enforcement actions are **app-loop level**: block a specific tool call, inject a warning into the agent's reasoning, redirect to original instructions, trigger human review, response shaping/sanitization.

**Closed loop:** Ascend findings → Defend policies → detections → back into Ascend tests.

## 3. Technical architecture — how it actually works

### Capture / integration (5 insertion modes)
SDK (OTLP one-line), API/hook/webhook, **inline gateway** (the real enforcement path), thin-client proxy, **eBPF sensor** (observe-only), browser extension. They market "no proxies/firewalls/thick clients" for the lightweight path — but the inline gateway/proxy is what actually blocks.

### Decision location
**Cloud.** Telemetry is shipped to Straiker's "**Detection Cloud**" where the models run. The SDK/sensor/gateway are thin capture-and-forward points. Decisioning is **not on-host**.

### Detection engine
"**Medley of Experts**" — an ensemble of **small, fine-tuned models** with **MoE routing + RLHF**, run **in parallel**, each specialized per attack pattern (general PI, persona/authority manipulation, encoding/obfuscation, hallucination, code injection, visual PI). Positioned against "legacy rule-based" and against "frontier model judges." Two-tier latency: **<130ms** classic patterns, **<300ms** agentic; claimed **98.1%** accuracy, **6–21× lower FP** than frontier judges (all self-reported).

### Enforcement model
- **Monitoring mode** (API integrations) = detect/alert only.
- **Inline gateway mode** = real-time block (the only true enforcement path).
- **eBPF sensor = observe only.**
- Actions are semantic/agent-loop interventions, **not** packet-level or in-kernel.

### Research (genuinely strong — STAR Labs)
Real published attack PoCs: EchoLeak (zero-click exfil), NomShub (Cursor tunnel), Perplexity Comet Drive wipe, GitHub MCP exploit, MCP DNS-rebinding, Yearbook jailbreak, Claude Code source-leak analysis. They co-authored/promote the **OWASP Agentic AI Top 10**.

## 4. Open source vs built-from-scratch — the technical verdict

Investigated via their GitHub, npm/PyPI, employees' public integration repos, job posts, and the CTO's patents.

| Layer | Build vs OSS | Confidence |
|---|---|---|
| **Telemetry SDK** (`straiker-telemetry==0.1.18`) | **Built on OSS** — thin wrapper over **stock OpenTelemetry** (OTLP-over-HTTP exporter, OTel instrumentation libs, `opentelemetry-semantic-conventions-ai` = Traceloop/OpenLLMetry GenAI conventions). Ships traces to `sink.stage.straiker.ai/v1/traces`. | **Confirmed** (their own demo repo's `requirements.txt` + `run_with_otel.py`) |
| **Inline gateway / enforcement** | **Built on OSS gateways** — plugins for **Kong** (Lua), **Portkey** (TS + Lambda adapter), **LiteLLM, Azure APIM**, all calling one HTTP contract `POST /api/v1/detect` (internal service code-named **"argus"**); gateway returns 403 if `score > threshold`. | **Confirmed** (employee repos `kong-plugin-straiker`, `portkey-adapter-straiker`) |
| **Attack-surface CLI** (`@strkr/stk` on npm) | OSS deps (axios/commander/ws); thin client | **Confirmed** |
| **eBPF "AI sensor"** | Internals **undisclosed** — no public BPF source, no evidence of Tetragon/Pixie/Falco/libbpf/bcc, no eBPF hiring signal. Cannot determine if custom or forked. | **Unknown** |
| **Detection "AI Engine"** | **Claimed proprietary.** They **fine-tune pretrained models** (job posts name GPT/BERT/T5) — **not** trained from scratch. Base model, sizes, training infra **not public**. | Claim confirmed; internals unknown |

**Headline:** Straiker's *plumbing* (capture + enforcement transport) is **standard open source** — OpenTelemetry + existing API gateways (Kong/Portkey/LiteLLM). Their *differentiator* is the proprietary detection-model cloud, whose internals aren't publicly verifiable. Their public GitHub org (`straiker-ai`) has **zero public repos** — everything core is private.

### Engineering stack (from job posts + patents)
- **Languages:** Python + Go (confirmed). No Rust evidence.
- **ML:** TensorFlow / PyTorch; fine-tune pretrained LLMs; ensemble + MoE + RLHF; RL + human-in-loop labeling fed by STAR red-team findings.
- **Cloud:** AWS (Marketplace vendor-hosted API; listed first in job posts).
- **The <300ms blueprint:** CTO Kurupati's Akamai patent (EP4008096A1) describes a tiny 4-layer NN with a two-stage design — heavy offline retraining + a cheap online real-time matcher. Almost certainly the intellectual basis for the small-model, low-latency "AI Engine."

## 5. Straiker vs us (Inferspect / Ollive)

| Dimension | Inferspect (us) | Straiker |
|---|---|---|
| **Capture layer** | In-kernel at **libssl** (uprobe) | App-layer SDK/OTLP + inline gateway; eBPF only as observe sensor |
| **Decision location** | **On-host / in-kernel** | **Cloud Detection Cloud** (telemetry shipped out) |
| **Enforcement** | **Kill/modify request in-kernel** | App-loop interventions via inline gateway; eBPF = no enforcement |
| **Deployment** | **Daemon-only, zero app change** | "One-line," but still needs a hook/SDK/gateway/OTLP in the path |
| **PII** | **Redacted on-host before egress** | Cloud-side; data leaves host |
| **Conversation identity** | **Content-derived** (prefix fingerprint + rolling hash) | Trace/session-based (OTLP spans) |
| **Detection sophistication** | Anchor/pattern + roadmap detectors | **Ensemble of fine-tuned models — clearly ahead** |
| **Red-teaming / pre-deploy** | none yet | **Ascend AI — mature** |
| **Insurance / risk transfer** | **Core thesis** | **None** |

### Where Straiker is clearly ahead
- Semantic **detection quality** (ensemble fine-tuned models on real agent traces) — we will not out-detect them quickly.
- **Lifecycle breadth** — discover + red-team + defend in one platform.
- **MCP/tool-poisoning** coverage as a first-class runtime threat.
- Funding, customers, brand, research output.

### Our genuine white space (things Straiker cannot currently claim)
1. **True daemon-only, in-kernel enforcement.** Their "no code change" still needs a hook/SDK/gateway in the request path; their eBPF is observe-only feeding a cloud judge. A transparent eBPF capture-and-kill with on-host decisioning is architecturally distinct and they (and Palo Alto/Lakera) cannot match it today.
2. **On-host PII redaction** — data never leaves the customer machine; Straiker ships telemetry to its cloud.
3. **Content-derived conversation identity** — no app cooperation; theirs is OTLP-span/session based.
4. **AI-liability insurance / risk transfer** — *nobody in this category offers it.* Converts security from cost center to risk-transfer instrument; a different buyer (CFO/risk, not just CISO).

### Where we'll be pressed
Depth of *what* we can semantically detect/block (PI nuance, MCP tool poisoning, multi-turn kill chains) and the lack of a pre-deployment red-teaming product — an eBPF data-plane alone doesn't provide those. The realistic posture: **win on deployment model (transparent + on-host) and risk-transfer (insurance); borrow detection logic** (LlamaFirewall/AgentDojo-style, per `agents-of-chaos.md`) rather than try to out-train their model cloud near-term.

## 6. Strategic takeaways

1. **"Reach their level" is two different bars.** On *detection ML* they're ahead and well-funded — matching that head-on is a multi-year model effort. On *deployment + enforcement + risk transfer* we already occupy ground they don't. Compete where we're differentiated, not where they're strongest.
2. **Their plumbing is OSS we can also use** — OpenTelemetry capture, gateway plugins. No moat there. The moat is the model cloud (theirs) and the in-kernel-enforcement + insurance (ours).
3. **Their eBPF is a visibility sensor, not enforcement.** This is the single most important technical correction to any "Straiker already does eBPF kill" assumption — they do not. Our in-kernel kill is unmatched in this set.
4. **No competitor ties to insurance.** That remains the clearest strategic wedge.

---

## Sources

Company / funding: straiker.ai/about; PRNewswire launch (302412224) + AI-100 (302762285); SecurityWeek; SiliconANGLE; Crunchbase; PitchBook.
Products: straiker.ai/products, /products/{discover,ascend,defend}-ai; AWS Marketplace prodview-4ey26i3k5epmi.
Technical: straiker.ai/blog/how-ai-integration-models-shape-security-for-agentic-applications (5 integration modes incl. eBPF/proxy); /research (STAR Labs); helpnetsecurity.com/2026/03/23/straiker-discover-ai; intellyx.com/2025/12/17 (observe-mode read); securityboulevard.com/2026/03.
Open-source evidence: github.com/straiker-ai (0 repos); github.com/PhimmStraiker/{kong-plugin-straiker, portkey-adapter-straiker, ai-sales-agent-app}; npmjs.com/package/@strkr/stk; pypi.org (no public straiker-telemetry).
Stack/model: LinkedIn Straiker "AI Engineer / Sr. AI Engineer" posting; straiker.ai/careers; patents EP4008096A1 / US20210037048A1 (Kurupati/Akamai), US 10,754,935.
Competitive: straiker.ai/blog/top-7-ai-runtime-security-platforms; generalanalysis.com guides; repello.ai/blog guides.
