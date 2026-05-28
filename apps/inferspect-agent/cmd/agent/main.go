// Inferspect agent entrypoint.
//
// Boots:
//  1. BPF programs (libssl uprobes + ringbuf)
//  2. HTTP reassembler (per-(pid,ssl_ctx) request stitching)
//  3. Uplink (batched POST /v1/logs)
//  4. Downlink (long-poll GET /v1/control/poll)
//  5. Heartbeat (one-shot POST to register the host)
//
// On Ctrl+C: drains the uplink buffer, closes BPF resources, exits.
package main

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"log"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/cilium/ebpf"
	"github.com/google/uuid"

	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/config"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/downlink"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/fingerprint"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/host"
	httpreasm "github.com/itsdun1/inferspect/apps/inferspect-agent/internal/http"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/kernel"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/llm"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/policy"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/redact"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/ringbuf"
	"github.com/itsdun1/inferspect/apps/inferspect-agent/internal/uplink"
)

// requestContext stashes everything from the request side so the response
// stitcher can emit a follow-up event with the SAME request_id and
// conversation_id.
type requestContext struct {
	startedAt      time.Time
	fingerprint    string
	requestID      string
	conversationID string
	provider       llm.Provider
	model          string
	stream         bool
	hostID         string
	containerID    string
	pid            uint32
	sslCtx         uint64
	// lastBody is the most-recently-observed wire body for this
	// connection. We keep it so the Phase G.4 kill-verifier has SOMETHING
	// to hash when EVT_SSL_KILL arrives (the kernel already corrupted the
	// in-flight buffer; user-space never gets a clean copy). The hash of
	// the prior turn won't match the expected hash for the new turn, so
	// in practice this almost always falls through to the "trust the
	// kernel slot" path inside AnchorStore.RecordKill — but the field is
	// retained for the case where the operator hashes against a prior
	// turn's wire shape (rare but supported).
	lastBody []byte
}

// requestStore maps (pid, ssl_ctx) → requestContext. Entries are removed when
// the matching response arrives or when a TTL sweeper notices them sitting
// idle past requestContextTTL.
type requestStore struct {
	mu  sync.Mutex
	ctx map[ctxKey]*requestContext
}

type ctxKey struct {
	pid uint32
	ssl uint64
}

const requestContextTTL = 5 * time.Minute

func newRequestStore() *requestStore {
	return &requestStore{ctx: make(map[ctxKey]*requestContext)}
}

func (s *requestStore) put(rc *requestContext) {
	s.mu.Lock()
	s.ctx[ctxKey{pid: rc.pid, ssl: rc.sslCtx}] = rc
	s.mu.Unlock()
}

func (s *requestStore) take(pid uint32, sslCtx uint64) (*requestContext, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	key := ctxKey{pid: pid, ssl: sslCtx}
	rc, ok := s.ctx[key]
	if ok {
		delete(s.ctx, key)
	}
	return rc, ok
}

// peekBody returns the last-observed wire body for (pid, ssl_ctx) without
// removing the entry. Used by the EVT_SSL_KILL handler to feed the
// anchor-verifier without disrupting the request/response stitch.
func (s *requestStore) peekBody(pid uint32, sslCtx uint64) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	rc, ok := s.ctx[ctxKey{pid: pid, ssl: sslCtx}]
	if !ok || rc == nil {
		return nil
	}
	return rc.lastBody
}

// sweepLoop removes idle entries on a fixed cadence so memory doesn't grow
// unbounded when a connection closes without us seeing a response (e.g. the
// client cancelled).
func (s *requestStore) sweepLoop(ctx context.Context) {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-tick.C:
			s.mu.Lock()
			for k, rc := range s.ctx {
				if now.Sub(rc.startedAt) > requestContextTTL {
					delete(s.ctx, k)
				}
			}
			s.mu.Unlock()
		}
	}
}

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	bpfObject := os.Getenv("INFERSPECT_BPF_OBJECT")
	if bpfObject == "" {
		bpfObject = "/usr/local/share/inferspect/ssl_uprobe.o"
	}

	loader, err := kernel.Load(bpfObject, cfg.LibSSLPaths)
	if err != nil {
		log.Fatalf("kernel: %v", err)
	}
	defer loader.Close()
	log.Printf("BPF loaded, uprobes attached to %s", loader.LibsslPath())

	hostInfo := host.Collect(cfg.HostID)
	log.Printf("host_id=%s kernel=%s btf=%v os=%s",
		hostInfo.HostID, hostInfo.Kernel, hostInfo.BTFAvailable, hostInfo.OSRelease)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go awaitShutdown(cancel)

	uplinkT := uplink.New(cfg.IngestionURL, cfg.APIKey, cfg.ServiceName, cfg.BatchInterval, cfg.BatchMaxEvents)
	go uplinkT.Run(ctx)

	// Heartbeat once on boot so the registry shows us immediately.
	go func() {
		if err := uplinkT.Heartbeat(ctx, hostInfo.HostID, map[string]any{
			"agent_version": cfg.AgentVersion,
			"kernel":        hostInfo.Kernel,
			"btf":           hostInfo.BTFAvailable,
			"libssl_path":   loader.LibsslPath(),
		}); err != nil {
			log.Printf("heartbeat: %v", err)
		}
	}()

	policyStore := policy.NewStore()

	// Phase G.3 conversation tracker: maintains rolling-hash chain per
	// conversation so we can recognize "this is turn N+1 of an existing
	// chat" without any customer-side cooperation. Bounded to 100K active
	// conversations, evicts entries idle > 1h. See internal/fingerprint/tracker.go.
	tracker := fingerprint.NewTracker(100_000, time.Hour)
	trackerStop := make(chan struct{})
	go tracker.SweepLoop(trackerStop)
	defer close(trackerStop)

	reader := ringbuf.New(loader.Ringbuf())
	blockedSSL := loader.BlockedSSLContexts()
	blockedPIDsMap := loader.BlockedPIDs()
	blockedAnchorsMap := loader.BlockedAnchors()

	// Phase G.4 — AnchorStore owns the kernel-side blocked_anchors map.
	// Inject a rolling-hash adapter so RecordKill can verify each kill
	// against the targeted conversation's expected hash without dragging
	// the fingerprint package into the policy import graph.
	policy.SetRollingHashFunc(func(buf []byte) ([32]byte, bool) {
		msgs, err := fingerprint.ExtractNormalizedMessages(buf)
		if err != nil || len(msgs) == 0 {
			return [32]byte{}, false
		}
		return fingerprint.ComputeRollingHashOver(msgs), true
	})
	anchorStore := policy.NewAnchorStore(blockedAnchorsMap)

	// Phase G.3 enforcement layer: PID-wide short-TTL block catches the race
	// where the customer's httpx pool opens a NEW SSL_ctx for the next
	// turn after a kill. Default 200ms window — long enough to cover pool
	// churn, short enough to limit collateral damage to other concurrent
	// conversations on the same PID.
	pidWindow := policy.NewPIDWindow(blockedPIDsMap, 200*time.Millisecond)
	pidWindowStop := make(chan struct{})
	go pidWindow.Run(pidWindowStop)
	defer close(pidWindowStop)

	// preArmKernel writes (pid, ssl_ctx) into blocked_ssl_contexts so the
	// VERY NEXT SSL_write on that connection gets corrupted. Without this,
	// the first write after a kill slips through (the agent would otherwise
	// only arm the map on observing the write).
	preArmKernel := func(pid uint32, sslCtx uint64) {
		if blockedSSL == nil {
			return
		}
		key := sslCtx
		val := uint8(1)
		if err := blockedSSL.Update(&key, &val, ebpf.UpdateAny); err != nil {
			log.Printf("policy: pre-arm failed pid=%d ssl=%x: %v", pid, sslCtx, err)
		} else {
			log.Printf("policy: pre-armed kernel-block on pid=%d ssl=%x", pid, sslCtx)
		}
	}

	poller := downlink.New(cfg.IngestionURL, cfg.APIKey, hostInfo.HostID, cfg.PollTimeout)
	go poller.Run(ctx, func(_ context.Context, cmd downlink.Command) error {
		switch cmd.Command {
		case "block_fingerprint":
			log.Printf("downlink: block_fingerprint %s (reason=%s ttl=%ds)",
				cmd.Fingerprint[:min(8, len(cmd.Fingerprint))]+"...", cmd.Reason, cmd.TTLSeconds)
			policyStore.Block(cmd.Fingerprint, cmd.TTLSeconds)
			// Pre-arm every active conversation matching this fingerprint
			// (sockets we've already observed). For brand-new sockets the
			// TLS pool may open between kill and next turn, enforcement
			// falls back to the user-space check on first observation —
			// known "1 turn delay" trade-off; see plan §5b.
			//
			// Intentionally NOT using a PID-wide window — would block
			// unrelated concurrent conversations on the same process.
			_ = pidWindow // PID-window code retained for future per-tenant isolated processes
			for _, ps := range tracker.SocketsForFingerprint(cmd.Fingerprint) {
				preArmKernel(uint32(ps[0]), ps[1])
			}
			// Phase G.4 PII path — backend sends only the fingerprint
			// (no raw user text, since input_preview is redacted before
			// uplink). Reconstruct the kernel content anchor from the
			// agent's host-local tracker so the next outbound SSL_write
			// carrying this conversation's first user message gets
			// caught regardless of SSL_CTX rotation.
			if firstUser := tracker.FirstUserTextForFingerprint(cmd.Fingerprint); firstUser != "" {
				anchorBytes := buildLocalAnchor(firstUser)
				if len(anchorBytes) >= 16 {
					if slot, err := anchorStore.Arm(cmd.CommandID, anchorBytes, [32]byte{}); err != nil {
						log.Printf("downlink: local anchor arm failed for fp=%s: %v",
							cmd.Fingerprint[:min(8, len(cmd.Fingerprint))], err)
					} else {
						log.Printf("downlink: local anchor armed cmd=%s slot=%d anchor=%dB (built from host-local tracker)",
							cmd.CommandID, slot, len(anchorBytes))
					}
				}
			}
		case "block_conversation":
			log.Printf("downlink: block_conversation %s (reason=%s ttl=%ds)",
				cmd.Fingerprint[:min(8, len(cmd.Fingerprint))]+"...", cmd.Reason, cmd.TTLSeconds)
			policyStore.Block(cmd.Fingerprint, cmd.TTLSeconds)
			if id, perr := uuid.Parse(cmd.Fingerprint); perr == nil {
				if pid, sslCtx, ok := tracker.SocketForAgentID(id); ok {
					preArmKernel(pid, sslCtx)
				}
			}
		case "unblock_fingerprint":
			policyStore.Unblock(cmd.Fingerprint)
			// Clear every BPF-side enforcement state for this fingerprint:
			// the SSL_CTX pre-arm map, the per-PID block (if any), and any
			// content-anchor slot armed for this fingerprint's first user
			// message. Otherwise the operator's "unblock" leaves the
			// kernel still corrupting writes — confusing and dangerous.
			cleared := 0
			for _, ps := range tracker.SocketsForFingerprint(cmd.Fingerprint) {
				sslCtx := ps[1]
				if blockedSSL != nil {
					_ = blockedSSL.Delete(&sslCtx)
					cleared++
				}
			}
			if firstUser := tracker.FirstUserTextForFingerprint(cmd.Fingerprint); firstUser != "" {
				wanted := buildLocalAnchor(firstUser)
				if slot := anchorStore.DisarmIfAnchorMatches(wanted); slot >= 0 {
					log.Printf("downlink: unblock_fingerprint cleared anchor slot=%d", slot)
				}
			}
			log.Printf("downlink: unblock_fingerprint %s — cleared %d SSL_ctx pre-arms",
				cmd.Fingerprint[:min(8, len(cmd.Fingerprint))], cleared)
		case "block_anchor":
			// Phase G.4 — operator-issued content-anchor block. The
			// payload carries the byte pattern (base64) the kernel
			// scans outgoing SSL_write buffers for, plus the rolling
			// hash the agent expects to see if the kill lands on the
			// targeted conversation. Mismatch counts as collateral.
			anchor, err := base64.StdEncoding.DecodeString(cmd.AnchorBase64)
			if err != nil || len(anchor) == 0 {
				log.Printf("downlink: block_anchor bad anchor_b64: %v", err)
				return nil
			}
			expectedBytes, err := base64.StdEncoding.DecodeString(cmd.ExpectedHashBase64)
			if err != nil || len(expectedBytes) != 32 {
				log.Printf("downlink: block_anchor bad expected_hash_b64 (len=%d): %v", len(expectedBytes), err)
				return nil
			}
			var expected [32]byte
			copy(expected[:], expectedBytes)
			slot, err := anchorStore.Arm(cmd.CommandID, anchor, expected)
			if err != nil {
				log.Printf("downlink: block_anchor arm failed: %v", err)
				return nil
			}
			log.Printf("downlink: block_anchor cmd=%s slot=%d anchor=%dB reason=%s",
				cmd.CommandID, slot, len(anchor), cmd.Reason)
		case "unblock_anchor":
			slot, err := anchorStore.DisarmByCommandID(cmd.CommandID)
			if err != nil {
				log.Printf("downlink: unblock_anchor cmd=%s failed: %v", cmd.CommandID, err)
				return nil
			}
			if slot < 0 {
				log.Printf("downlink: unblock_anchor cmd=%s — no armed slot", cmd.CommandID)
			} else {
				log.Printf("downlink: unblock_anchor cmd=%s slot=%d cleared", cmd.CommandID, slot)
			}
		default:
			log.Printf("downlink: unsupported command %q", cmd.Command)
		}
		return nil
	})

	// Phase G.4 — periodic uplink kill-report. Every 30s, snapshot the
	// AnchorStore's per-slot confirmed/collateral counters and ship them
	// as a single ``kill_report`` event. One ticker, no new goroutines
	// beyond this one, no per-event uplink chatter (the per-kill log
	// row is already going via buildKillEvent).
	go runKillReportLoop(ctx, anchorStore, uplinkT, hostInfo)

	reasm := httpreasm.NewReassembler()
	store := newRequestStore()
	go store.sweepLoop(ctx)

	if err := reader.Run(ctx, func(ev ringbuf.Event) {
		switch ev.Type {
		case ringbuf.EvtSSLKill:
			// Phase G.4 — verify the kill against the armed anchors.
			// AnchorSlot in the event is biased (+1 by the kernel); -1
			// here means "no anchor scan involvement, kill came from
			// SSL_ctx / PID map".
			kernelSlot := int(ev.AnchorSlot) - 1
			body := store.peekBody(ev.PID, ev.SSLCtx)
			matched, confirmed := anchorStore.RecordKill(body, kernelSlot)
			log.Printf("kill applied: kernel corrupted SSL_write on pid=%d ssl=%x (size=%d) "+
				"kernel_slot=%d matched_slot=%d confirmed=%t",
				ev.PID, ev.SSLCtx, ev.Total, kernelSlot, matched, confirmed)
			uplinkT.Enqueue(buildKillEvent(ev, hostInfo, matched, confirmed))
			return
		case ringbuf.EvtSSLWrite:
			if len(ev.Payload) == 0 {
				return
			}
			requests := reasm.Feed(ev.PID, ev.SSLCtx, ev.Payload)
			for _, req := range requests {
				handleRequest(req, hostInfo, policyStore, uplinkT, blockedSSL, store, tracker, anchorStore)
			}
		case ringbuf.EvtSSLRead:
			if len(ev.Payload) == 0 {
				return
			}
			responses := reasm.FeedResponse(ev.PID, ev.SSLCtx, ev.Payload)
			for _, resp := range responses {
				handleResponse(resp, uplinkT, store, tracker)
			}
		}
	}); err != nil {
		log.Printf("ringbuf: %v", err)
	}
	log.Printf("agent shutdown")
}

func handleRequest(
	req httpreasm.Request,
	hostInfo host.Info,
	store *policy.Store,
	tx *uplink.Transport,
	blockedSSL *ebpf.Map,
	rs *requestStore,
	tracker *fingerprint.Tracker,
	anchorStore *policy.AnchorStore,
) {
	provider := llm.IdentifyByHost(req.Host)
	if provider == llm.ProviderUnknown || len(req.Body) == 0 {
		return
	}

	var fp string
	var err error
	switch provider {
	case llm.ProviderOpenAI:
		fp, err = fingerprint.FromOpenAIBody(req.Body)
	case llm.ProviderAnthropic:
		fp, err = fingerprint.FromAnthropicBody(req.Body)
	default:
		// Google not yet supported by fingerprinter.
		fp = ""
	}
	if err != nil {
		log.Printf("fingerprint failed for %s: %v", provider, err)
	}

	model := llm.ExtractModel(req.Body)
	stream := llm.ExtractStream(req.Body)

	shortFP := fp
	if len(shortFP) > 16 {
		shortFP = shortFP[:16] + "..."
	}
	log.Printf("captured %s/%s pid=%d fingerprint=%s body=%dB",
		provider, model, req.PID, shortFP, len(req.Body))

	startedAt := time.Now().UTC()
	requestID := deriveRequestID(req.SSLCtx, startedAt.UnixNano()).String()

	// Phase G.3 — resolve conversation identity via the rolling-hash tracker.
	// This recognizes "turn N+1 of an existing chat" by matching the
	// message-chain predecessor, so two parallel conversations that started
	// identically still get separate AgentIDs once they diverge.
	convID := ""
	if msgs, err := fingerprint.ExtractNormalizedMessages(req.Body); err == nil && len(msgs) > 0 {
		convID = tracker.IdentifyOrCreate(req.PID, req.SSLCtx, fp, msgs).String()
	} else if fp != "" {
		// Fallback for providers we can parse a prefix for but not a full
		// chain (or when extraction failed): use the deterministic
		// prefix-derived uuid so the row still carries SOME conv_id.
		convID = uuid.NewSHA1(uuid.NameSpaceOID, []byte(fp)).String()
	}
	containerID := os.Getenv("INFERSPECT_CONTAINER_ID")

	// Stash so the response-side handler can emit a follow-up event with the
	// same request_id. ClickHouse's ReplacingMergeTree dedups on request_id;
	// the later (more-complete) row wins.
	//
	// lastBody is held alongside so the EVT_SSL_KILL handler can verify
	// the kernel's anchor strike landed on this conversation (Phase G.4).
	// We snapshot rather than reference req.Body — the reassembler reuses
	// its buffer.
	bodyCopy := append([]byte(nil), req.Body...)
	rs.put(&requestContext{
		startedAt:      startedAt,
		fingerprint:    fp,
		requestID:      requestID,
		conversationID: convID,
		provider:       provider,
		model:          model,
		stream:         stream,
		hostID:         hostInfo.HostID,
		containerID:    containerID,
		pid:            req.PID,
		sslCtx:         req.SSLCtx,
		lastBody:       bodyCopy,
	})

	// Phase G.4 Layer 2 — user-space substring verifier. The kernel only
	// scans the first SCAN_WINDOW (512) bytes; long system prompts or
	// deep conversation chains push the killed text past that boundary.
	// We get the FULL reassembled body here, so a Go-side bytes.Index
	// finds the anchor regardless of position. On hit, we pre-arm the
	// kernel blocked_ssl_contexts map for this connection so the next
	// outbound SSL_write on the same SSL_CTX is corrupted in-kernel.
	// Trade-off: the current turn isn't blocked (its SSL_write already
	// fired before we got here); the NEXT one is. For "kill in-flight
	// hallucination", that's acceptable — typical streamed responses
	// take seconds, the next turn fails immediately after.
	if anchorStore != nil && blockedSSL != nil {
		if slot, matched, cmdID := anchorStore.MatchBody(req.Body); slot >= 0 {
			log.Printf("layer2: anchor match cmd=%s slot=%d anchor=%dB body=%dB — pre-arming SSL_ctx=%x",
				cmdID, slot, len(matched), len(req.Body), req.SSLCtx)
			key := req.SSLCtx
			val := uint8(1)
			if err := blockedSSL.Update(&key, &val, ebpf.UpdateAny); err != nil {
				log.Printf("layer2: pre-arm failed pid=%d ssl=%x: %v", req.PID, req.SSLCtx, err)
			}
		}
	}

	// Build a synthetic InferenceLog. We don't have the response yet — the
	// response-side handler emits a follow-up event when SSL_read produces a
	// complete HTTP response.
	event := uplink.Event{
		"log_type":        "inference",
		"schema_version":  "1.0",
		"service":         "ebpf-agent",
		"sdk_version":     "ebpf-agent-0.1.0",
		"request_id":      requestID,
		"conversation_id": convID,
		"provider":        string(provider),
		"model":           model,
		"stream":          stream,
		"started_at":      startedAt.Format(time.RFC3339Nano),
		"finished_at":     startedAt.Format(time.RFC3339Nano),
		"latency_ms":      0,
		"status":          "ok",
		"input_preview":   redact.Text(safePreview(req.Body, 16384)),
		"output_preview":  "",
		"source":          "ebpf-agent",
		"host_id":         hostInfo.HostID,
		"process_id":      int(req.PID),
		"container_id":    containerID,
		"fingerprint":     fp,
		"metadata": map[string]any{
			"ssl_ctx":       strconv.FormatUint(req.SSLCtx, 16),
			"libssl_path":   hostInfo.OSRelease,
			"agent_runtime": "ebpf-uprobe",
		},
	}
	tx.Enqueue(event)

	// Phase G.2/G.3 enforcement: the operator may have issued a block by
	// fingerprint (pattern) OR by conversation_id (specific chat). We check
	// both — either match arms the kernel-side disruption for the NEXT
	// SSL_write on this same connection.
	blocked := ""
	switch {
	case fp != "" && store.IsBlocked(fp):
		blocked = "fingerprint=" + safeShort(fp, 16)
	case convID != "" && store.IsBlocked(convID):
		blocked = "conversation_id=" + safeShort(convID, 16)
	}
	if blocked != "" {
		if blockedSSL != nil {
			key := req.SSLCtx
			val := uint8(1)
			if err := blockedSSL.Update(&key, &val, ebpf.UpdateAny); err != nil {
				log.Printf("policy: failed to mark ssl_ctx %x for kernel block: %v", req.SSLCtx, err)
			} else {
				log.Printf("policy: armed kernel-block on pid=%d ssl=%x for %s",
					req.PID, req.SSLCtx, blocked)
			}
		} else {
			log.Printf("policy: observed blocked %s on pid=%d ssl=%x (BPF map unavailable)",
				blocked, req.PID, req.SSLCtx)
		}
	}
}

// safeShort truncates s to n+3 chars with "..." suffix.
func safeShort(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

func handleResponse(resp httpreasm.Response, tx *uplink.Transport, rs *requestStore, tracker *fingerprint.Tracker) {
	rc, ok := rs.take(resp.PID, resp.SSLCtx)
	if !ok {
		// No matching request context — either this is an upstream that we
		// don't fingerprint (non-LLM traffic) or the request was emitted
		// before the agent attached. Either way, nothing to stitch.
		return
	}

	var (
		usage  llm.Usage
		finish llm.FinishReason
		output llm.OutputText
		err    error
	)
	switch rc.provider {
	case llm.ProviderOpenAI:
		usage, finish, output, err = llm.ParseOpenAIResponse(resp.Body)
	case llm.ProviderAnthropic:
		usage, finish, output, err = llm.ParseAnthropicResponse(resp.Body)
	default:
		return
	}
	if err != nil {
		// Streaming responses, SSEs, partial bodies — log and move on. The
		// request-time row still lands in ClickHouse with latency_ms=0.
		log.Printf("response parse failed for %s/%s pid=%d: %v",
			rc.provider, rc.model, rc.pid, err)
		return
	}

	now := time.Now().UTC()
	latencyMs := now.Sub(rc.startedAt).Milliseconds()
	status := "ok"
	if resp.StatusCode >= 400 {
		status = "error"
	}

	event := uplink.Event{
		"log_type":          "inference",
		"schema_version":    "1.0",
		"service":           "ebpf-agent",
		"sdk_version":       "ebpf-agent-0.1.0",
		"request_id":        rc.requestID,
		"conversation_id":   rc.conversationID,
		"provider":          string(rc.provider),
		"model":             rc.model,
		"stream":            rc.stream,
		"started_at":        rc.startedAt.Format(time.RFC3339Nano),
		"finished_at":       now.Format(time.RFC3339Nano),
		"latency_ms":        int(latencyMs),
		"status":            status,
		"finish_reason":     string(finish),
		// Preserve the request body in input_preview so the backend's
		// session_fingerprint query can extract the kill anchor — without
		// this, ClickHouse's ReplacingMergeTree dedupe on request_id keeps
		// the stitched row (later received_at) and overwrites the
		// request-time row whose preview we actually need.
		"input_preview":     redact.Text(safePreview(rc.lastBody, 16384)),
		"output_preview":    redact.Text(safePreview([]byte(output), 500)),
		"prompt_tokens":     usage.Prompt,
		"completion_tokens": usage.Completion,
		"total_tokens":      usage.Total,
		"source":            "ebpf-agent",
		"host_id":           rc.hostID,
		"process_id":        int(rc.pid),
		"container_id":      rc.containerID,
		"fingerprint":       rc.fingerprint,
		// http_status doesn't exist in InferenceLog (extra='forbid'); tuck it
		// under metadata so the schema accepts the row.
		"metadata": map[string]any{
			"ssl_ctx":       strconv.FormatUint(rc.sslCtx, 16),
			"agent_runtime": "ebpf-uprobe",
			"stitched":      true,
			"http_status":   resp.StatusCode,
		},
	}
	tx.Enqueue(event)

	// Phase G.3 — fold the assistant's reply into the tracker's rolling hash
	// for this conversation, so the NEXT turn's predecessor lookup matches.
	// Without this step, the wire body for turn N+1 includes assistant_N
	// in messages[:-1], but our stored hash wouldn't have it, and the
	// predecessor lookup would miss and mint a (wrong) new conversation.
	if rc.conversationID != "" {
		if convUUID, perr := uuid.Parse(rc.conversationID); perr == nil {
			tracker.AppendAssistant(convUUID, string(output))
		}
	}

	log.Printf("stitched response %s/%s tokens=%d/%d/%d latency=%dms finish=%s",
		rc.provider, rc.model, usage.Prompt, usage.Completion, usage.Total,
		latencyMs, finish)
}

// buildKillEvent shapes a synthetic inference event recording that a kernel
// block fired. The backend uses these to flip enforcement_events.matched=1.
// matchedSlot is the anchor slot the user-space verifier attributed the kill
// to (-1 = none); confirmed is true when that match aligns with the
// kernel-reported slot (or any armed anchor for the legacy SSL_ctx/PID
// paths).
func buildKillEvent(ev ringbuf.Event, hostInfo host.Info, matchedSlot int, confirmed bool) uplink.Event {
	_ = binary.LittleEndian // keep the import live for future struct decoding
	kernelSlot := int(ev.AnchorSlot) - 1
	return uplink.Event{
		"log_type":       "inference",
		"schema_version": "1.0",
		"service":        "ebpf-agent",
		"sdk_version":    "ebpf-agent-0.1.0",
		"request_id":     uuid.NewSHA1(uuid.NameSpaceOID, []byte(strconv.FormatUint(ev.SSLCtx, 16)+strconv.FormatInt(time.Now().UnixNano(), 10))).String(),
		"provider":       "blocked",
		"model":          "blocked",
		"stream":         false,
		"started_at":     time.Now().UTC().Format(time.RFC3339Nano),
		"finished_at":    time.Now().UTC().Format(time.RFC3339Nano),
		"latency_ms":     0,
		"status":         "cancelled",
		"finish_reason":  "cancelled",
		"input_preview":  "[kernel-blocked SSL_write — buffer corrupted before send]",
		"output_preview": "",
		"source":         "ebpf-agent",
		"host_id":        hostInfo.HostID,
		"process_id":     int(ev.PID),
		"container_id":   os.Getenv("INFERSPECT_CONTAINER_ID"),
		"fingerprint":    "",
		"metadata": map[string]any{
			"agent_runtime":      "ebpf-uprobe",
			"event":              "kill_applied",
			"ssl_ctx":            strconv.FormatUint(ev.SSLCtx, 16),
			"original_size":      int(ev.Total),
			"kernel_anchor_slot": kernelSlot,
			"matched_slot":       matchedSlot,
			"confirmed":          confirmed,
		},
	}
}

// runKillReportLoop ticks every 30s and emits a single kill_report event
// summarising per-slot confirmed/collateral counts. Cheap: one Snapshot()
// (mutex held briefly) + one uplink Enqueue per tick.
func runKillReportLoop(ctx context.Context, store *policy.AnchorStore, tx *uplink.Transport, hostInfo host.Info) {
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			snap := store.Snapshot()
			if len(snap) == 0 {
				continue
			}
			slots := make([]map[string]any, 0, len(snap))
			for _, s := range snap {
				slots = append(slots, map[string]any{
					"slot":       s.Slot,
					"command_id": s.CommandID,
					"anchor_len": s.AnchorLen,
					"confirmed":  s.Confirmed,
					"collateral": s.Collateral,
				})
			}
			tx.Enqueue(uplink.Event{
				"log_type":       "kill_report",
				"schema_version": "1.0",
				"service":        "ebpf-agent",
				"sdk_version":    "ebpf-agent-0.1.0",
				"host_id":        hostInfo.HostID,
				"reported_at":    time.Now().UTC().Format(time.RFC3339Nano),
				"total_kills":    store.TotalKills(),
				"slots":          slots,
				"source":         "ebpf-agent",
			})
		}
	}
}

// deriveRequestID makes a stable UUID from (ssl_ctx, started_at_nanos) so the
// request-time event and the response-time event share an id. ClickHouse's
// ReplacingMergeTree dedups on request_id; the later row replaces the
// earlier.
func deriveRequestID(sslCtx uint64, startedAtNanos int64) uuid.UUID {
	seed := make([]byte, 16)
	for i, v := range []uint64{sslCtx, uint64(startedAtNanos)} {
		seed[i*8+0] = byte(v)
		seed[i*8+1] = byte(v >> 8)
		seed[i*8+2] = byte(v >> 16)
		seed[i*8+3] = byte(v >> 24)
		seed[i*8+4] = byte(v >> 32)
		seed[i*8+5] = byte(v >> 40)
		seed[i*8+6] = byte(v >> 48)
		seed[i*8+7] = byte(v >> 56)
	}
	return uuid.NewSHA1(uuid.NameSpaceOID, seed)
}

func safePreview(b []byte, n int) string {
	if len(b) > n {
		return string(b[:n])
	}
	return string(b)
}

// buildLocalAnchor constructs the kernel-side content anchor from the
// raw first-user-message text, padded with the JSON wrapper context the
// wire body uses so short messages still meet the BPF scan's
// FIRST_SEG (16-byte) minimum. Mirrors the build_anchor() logic in the
// backend's insights_api/services/anchor.py for the case where the
// backend itself has the raw preview — but now lives on-host so PII
// never leaves the customer machine.
func buildLocalAnchor(firstUser string) []byte {
	const minBytes = 16
	const maxBytes = 128
	raw := []byte(firstUser)
	if len(raw) >= minBytes {
		if len(raw) > maxBytes {
			raw = raw[:maxBytes]
		}
		return raw
	}
	wrapped := []byte(`"content":"` + firstUser + `"`)
	if len(wrapped) > maxBytes {
		wrapped = wrapped[:maxBytes]
	}
	return wrapped
}

func awaitShutdown(cancel context.CancelFunc) {
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	<-ch
	log.Printf("shutdown signal received")
	cancel()
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// hexShort returns the first n hex chars of b — used to keep log lines tight.
func hexShort(b []byte, n int) string { //nolint:unused — kept for future use
	s := hex.EncodeToString(b)
	if len(s) <= n {
		return s
	}
	return s[:n]
}
