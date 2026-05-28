package fingerprint

// Tracker — stateful per-process conversation identity, derived from
// observing how successive HTTPS request bodies extend the message chain.
//
// Each customer chat-completions request carries the FULL message history
// (system, user, assistant, user, ...). Turn N+1's body == turn N's body
// with one or more new messages appended. The tracker exploits this:
//
//   1. Compute a rolling hash of the message chain in the incoming body.
//   2. Compute the "predecessor hash" — the rolling hash up to (but not
//      including) the latest message.
//   3. Look the predecessor hash up in an in-memory map. If hit, this is
//      the next turn of a conversation we already gave an Agent ID to.
//   4. On miss, mint a new Agent ID for this conversation.
//
// Memory cost is ~128 bytes per active conversation (Agent ID + rolling
// hash + 32 bytes of map overhead). 100K active conversations on one host
// is ~13 MB. Per-request CPU is O(message-chain-length) SHA-256 ops —
// microseconds in practice. See docs/Phase-G plan §4b for the design
// trade-offs vs proxy-intercept and SDK-cooperation alternatives.

import (
	"crypto/sha256"
	"encoding/binary"
	"sync"
	"time"

	"github.com/google/uuid"
)

// NormalizedMessage is the shape the tracker hashes over. The caller is
// responsible for whitespace-collapse and role-lowercase via the existing
// canonicalize.go logic.
type NormalizedMessage struct {
	Role    string
	Content string
}

// Conversation is the per-conversation state held in memory.
type Conversation struct {
	AgentID        uuid.UUID
	RollingHash    [32]byte
	TurnCount      uint32
	PID            uint32
	LastActivityNs int64
	// LastSSLCtx is the SSL_CTX pointer the last write on this conversation
	// went through. When the operator kills the conversation, we use this to
	// pre-arm the kernel blocked_ssl_contexts map — closing the race window
	// where the first turn after a kill would otherwise slip through.
	LastSSLCtx uint64
	// PrefixFingerprint is the SHA256 of (system + first_user) — set at
	// conversation creation. Lets the agent answer "what conversations
	// match this fingerprint" so a kill-by-pattern can pre-arm too.
	PrefixFingerprint string
	// FirstUserText is the raw text of the conversation's first user
	// message. Host-local only — NEVER ships off the host. Used by the
	// downlink handler to build the kernel content-anchor when the
	// operator issues a block_fingerprint kill. Keeping the raw bytes
	// only on the customer machine is what lets us redact input_preview
	// before uplink while still preserving the kill capability.
	FirstUserText string
}

// Tracker holds the live conversation set.
type Tracker struct {
	mu sync.Mutex
	// Map keyed by RollingHash → conversation. The key changes every turn
	// (the new hash supersedes the old one), so updates always do a
	// delete-old / insert-new pair.
	byHash map[[32]byte]*Conversation
	// Secondary index used by AppendAssistant — when the response-stitcher
	// has an AgentID and wants to fold in the just-seen assistant reply.
	byAgentID map[uuid.UUID]*Conversation

	// Cap and TTL keep the map bounded. Default 100K convs / 1h idle.
	maxConversations int
	idleTTL          time.Duration

	// Stats — useful for debugging memory pressure on big fleets.
	created uint64
	evicted uint64
	matched uint64
}

// NewTracker constructs a tracker. ``max`` of 0 means unbounded (don't do
// this in production). ``idleTTL`` of 0 means no TTL sweep.
func NewTracker(max int, idleTTL time.Duration) *Tracker {
	return &Tracker{
		byHash:           make(map[[32]byte]*Conversation),
		byAgentID:        make(map[uuid.UUID]*Conversation),
		maxConversations: max,
		idleTTL:          idleTTL,
	}
}

// IdentifyOrCreate is the workhorse. It takes the parsed canonical message
// chain, the source PID, the SSL_CTX pointer, and the prefix fingerprint
// (SHA256 of system + first_user). Returns the Agent ID for the
// conversation this request belongs to.
//
// Continuation detection rule:
//
//   - len(messages) >= 4 AND second-to-last is "assistant" AND last is "user"
//     → look up the predecessor hash. On hit, extend. On miss, new conv.
//   - Otherwise (turn 1, or unusual shape) → new conv.
//
// The "have at least one assistant message" rule prevents every empty-history
// turn-1 from matching a shared "[system]" predecessor and collapsing
// into one conversation.
func (t *Tracker) IdentifyOrCreate(pid uint32, sslCtx uint64, prefixFP string, messages []NormalizedMessage) uuid.UUID {
	t.mu.Lock()
	defer t.mu.Unlock()

	nowNs := time.Now().UnixNano()

	if !looksLikeContinuation(messages) {
		conv := t.mintNew(pid, sslCtx, prefixFP, messages, nowNs)
		return conv.AgentID
	}

	predHash := rollingHashOver(messages[:len(messages)-1])
	conv, ok := t.byHash[predHash]
	if !ok || conv.PID != pid {
		// No existing chain ends in this predecessor — either we never saw
		// the earlier turns (agent restarted, or the predecessor was
		// evicted), or the customer rewrote message history. Treat as new.
		newConv := t.mintNew(pid, sslCtx, prefixFP, messages, nowNs)
		return newConv.AgentID
	}

	// Extend: delete old key, recompute full hash including the new tail
	// message, insert under the new key. Update the SSL_CTX too — the
	// connection might have changed if the client's pool reassigned slots.
	delete(t.byHash, predHash)
	newHash := rollHash(predHash, messages[len(messages)-1])
	conv.RollingHash = newHash
	conv.TurnCount++
	conv.LastActivityNs = nowNs
	conv.LastSSLCtx = sslCtx
	t.byHash[newHash] = conv
	t.matched++
	return conv.AgentID
}

// mintNew creates a fresh conversation, performs LRU eviction if capped.
func (t *Tracker) mintNew(pid uint32, sslCtx uint64, prefixFP string, messages []NormalizedMessage, nowNs int64) *Conversation {
	if t.maxConversations > 0 && len(t.byHash) >= t.maxConversations {
		t.evictOneLocked()
	}
	full := rollingHashOver(messages)
	// Capture the first user message's raw text — host-local material that
	// the downlink kill handler will need to build a content anchor.
	firstUser := ""
	for _, m := range messages {
		if m.Role == "user" {
			firstUser = m.Content
			break
		}
	}
	conv := &Conversation{
		AgentID:           uuid.New(),
		RollingHash:       full,
		TurnCount:         1,
		PID:               pid,
		LastActivityNs:    nowNs,
		LastSSLCtx:        sslCtx,
		PrefixFingerprint: prefixFP,
		FirstUserText:     firstUser,
	}
	t.byHash[full] = conv
	t.byAgentID[conv.AgentID] = conv
	t.created++
	return conv
}

// FirstUserTextForFingerprint returns the raw first user-message text of
// the most recently active conversation with the given prefix fingerprint.
// Empty string if no live conversation matches. The downlink handler uses
// this to build the kernel content anchor without the backend ever
// seeing the raw bytes — the operator's kill command carries only the
// fingerprint.
func (t *Tracker) FirstUserTextForFingerprint(fp string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	var latest *Conversation
	for _, c := range t.byAgentID {
		if c.PrefixFingerprint != fp {
			continue
		}
		if latest == nil || c.LastActivityNs > latest.LastActivityNs {
			latest = c
		}
	}
	if latest == nil {
		return ""
	}
	return latest.FirstUserText
}

// SocketsForFingerprint returns the (PID, SSL_CTX) of every conversation
// whose prefix-fingerprint matches. Used by the downlink handler to
// pre-arm the kernel kill on the moment a block_fingerprint command
// arrives — without this, the FIRST write after a kill slips through.
func (t *Tracker) SocketsForFingerprint(fp string) [][2]uint64 {
	t.mu.Lock()
	defer t.mu.Unlock()
	var out [][2]uint64
	for _, c := range t.byAgentID {
		if c.PrefixFingerprint == fp {
			out = append(out, [2]uint64{uint64(c.PID), c.LastSSLCtx})
		}
	}
	return out
}

// SocketForAgentID returns the (PID, SSL_CTX) for a specific conversation
// AgentID. Used by the downlink handler for precise per-conversation
// kills.
func (t *Tracker) SocketForAgentID(id uuid.UUID) (pid uint32, sslCtx uint64, ok bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	c, found := t.byAgentID[id]
	if !found {
		return 0, 0, false
	}
	return c.PID, c.LastSSLCtx, true
}

// AppendAssistant folds the just-observed assistant response into the
// conversation's rolling hash. Called from the response stitcher once it
// has parsed an OpenAI/Anthropic completion. Without this, the next turn's
// request body (which DOES include the assistant message in `messages`)
// would compute a predecessor hash that doesn't match the conversation's
// stored hash, and the tracker would think it's a brand-new conversation.
func (t *Tracker) AppendAssistant(agentID uuid.UUID, content string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	conv, ok := t.byAgentID[agentID]
	if !ok {
		return
	}
	delete(t.byHash, conv.RollingHash)
	newHash := rollHash(conv.RollingHash, NormalizedMessage{
		Role:    "assistant",
		Content: normalizeContent(content),
	})
	conv.RollingHash = newHash
	conv.LastActivityNs = time.Now().UnixNano()
	t.byHash[newHash] = conv
}

// evictOneLocked drops the single oldest entry. Lock must be held.
func (t *Tracker) evictOneLocked() {
	var oldestKey [32]byte
	var oldestConv *Conversation
	oldestNs := int64(1<<62 - 1)
	for k, c := range t.byHash {
		if c.LastActivityNs < oldestNs {
			oldestNs = c.LastActivityNs
			oldestKey = k
			oldestConv = c
		}
	}
	if oldestConv != nil {
		delete(t.byHash, oldestKey)
		delete(t.byAgentID, oldestConv.AgentID)
		t.evicted++
	}
}

// SweepLoop walks the map every minute and evicts entries idle past
// idleTTL. Runs until ctx is cancelled. Safe to omit (idle eviction is
// optional — the size cap handles the worst case).
func (t *Tracker) SweepLoop(stop <-chan struct{}) {
	if t.idleTTL == 0 {
		return
	}
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			t.sweepOnce()
		}
	}
}

func (t *Tracker) sweepOnce() {
	cutoff := time.Now().Add(-t.idleTTL).UnixNano()
	t.mu.Lock()
	defer t.mu.Unlock()
	for k, c := range t.byHash {
		if c.LastActivityNs < cutoff {
			delete(t.byHash, k)
			delete(t.byAgentID, c.AgentID)
			t.evicted++
		}
	}
}

// Size reports the current number of tracked conversations. Useful for a
// /agents/{host}/tracker-stats debug endpoint later.
func (t *Tracker) Size() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.byHash)
}

// Stats returns counters; caller does its own diffing for rate observation.
func (t *Tracker) Stats() (created, evicted, matched uint64, size int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.created, t.evicted, t.matched, len(t.byHash)
}

// looksLikeContinuation returns true when the message shape is "previous
// turn happened" — i.e. there's at least one assistant reply already in
// the chain and the latest message is the user's new turn.
func looksLikeContinuation(messages []NormalizedMessage) bool {
	if len(messages) < 4 {
		return false
	}
	last := messages[len(messages)-1]
	prev := messages[len(messages)-2]
	if last.Role != "user" {
		return false
	}
	if prev.Role != "assistant" {
		return false
	}
	return true
}

// ComputeRollingHashOver is the exported wrapper around rollingHashOver.
// Used by AnchorStore.RecordKill (Phase G.4) to recompute the wire body's
// rolling hash so user-space can confirm that a kernel-issued kill landed
// on the conversation the operator targeted.
func ComputeRollingHashOver(messages []NormalizedMessage) [32]byte {
	return rollingHashOver(messages)
}

// rollingHashOver computes H_n = SHA256(H_{n-1} || canonical(message_n))
// from the empty seed. Returns the 32-byte rolling hash.
func rollingHashOver(messages []NormalizedMessage) [32]byte {
	var h [32]byte // start from all-zeros sentinel
	for _, m := range messages {
		h = rollHash(h, m)
	}
	return h
}

// rollHash mixes one new message into a running hash. Domain-separated
// from any future use of plain SHA-256 by prefixing a 1-byte tag and the
// role length.
func rollHash(prev [32]byte, m NormalizedMessage) [32]byte {
	s := sha256.New()
	s.Write(prev[:])
	s.Write([]byte{0x01}) // domain tag
	var lenbuf [4]byte
	binary.LittleEndian.PutUint32(lenbuf[:], uint32(len(m.Role)))
	s.Write(lenbuf[:])
	s.Write([]byte(m.Role))
	binary.LittleEndian.PutUint32(lenbuf[:], uint32(len(m.Content)))
	s.Write(lenbuf[:])
	s.Write([]byte(m.Content))
	var out [32]byte
	copy(out[:], s.Sum(nil))
	return out
}
