// Package downlink handles the control channel from ingestion to the agent.
// Single in-flight long-poll GET, exponential backoff on errors, command
// dispatch table per command kind.
package downlink

// Command is the wire shape ingestion publishes to the per-host queue.
type Command struct {
	Command    string `json:"command"`
	CommandID  string `json:"command_id"`
	Reason     string `json:"reason,omitempty"`
	IssuedAt   string `json:"issued_at,omitempty"`
	TTLSeconds int    `json:"ttl_seconds,omitempty"`

	// block_fingerprint specific
	Fingerprint string `json:"fingerprint,omitempty"`

	// block_pid specific
	PID uint32 `json:"pid,omitempty"`

	// Phase G.4 — block_anchor / unblock_anchor.
	// AnchorBase64 is the byte pattern (≤ ANCHOR_MAX = 256 bytes) the
	// kernel scans outgoing SSL_write buffers for. ExpectedHashBase64 is
	// the 32-byte rolling hash the user-space verifier compares against
	// the captured buffer after a kill, so we can attribute it to either
	// "confirmed" (matches the target conversation) or "collateral"
	// (anchor matched but rolling hash diverged).
	AnchorBase64       string `json:"anchor_b64,omitempty"`
	ExpectedHashBase64 string `json:"expected_hash_b64,omitempty"`
}

type PollResponse struct {
	HostID   string    `json:"host_id"`
	Commands []Command `json:"commands"`
	Cursor   string    `json:"cursor"`
}
