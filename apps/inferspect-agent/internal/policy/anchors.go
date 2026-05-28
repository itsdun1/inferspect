// Package policy — Phase G.4 anchor store.
//
// AnchorStore is the user-space owner of the kernel-side ``blocked_anchors``
// BPF array map. Each slot pairs a byte pattern (the anchor — what the
// kernel scans the outgoing SSL_write for) with an expected rolling hash
// (what the user-space verifier checks the captured buffer against, so we
// can tell a confirmed kill from a collateral one).
//
// Wire shape mirrors the C struct ``anchor_entry`` in bpf/ssl_uprobe.c —
// keep field order and sizes identical or the map updates will scribble
// garbage.

package policy

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/cilium/ebpf"
)

var _bytesIndex = bytes.Index

// Mirrors of the BPF constants in bpf/ssl_uprobe.c. Keep in lockstep.
const (
	anchorMax  = 128
	maxAnchors = 8
)

// bpfAnchorEntrySize is the on-the-wire (and in-map) byte size of struct
// anchor_entry { __u32 len; __u8 bytes[ANCHOR_MAX]; }. The 4-byte length
// is followed by the byte array — no padding because alignof(__u8) is 1.
const bpfAnchorEntrySize = 4 + anchorMax

// SlotStats is the Snapshot() return shape — flat, JSON-friendly, no map
// handles or mutexes leak out.
type SlotStats struct {
	Slot       int    `json:"slot"`
	CommandID  string `json:"command_id"`
	AnchorLen  int    `json:"anchor_len"`
	Confirmed  uint64 `json:"confirmed"`
	Collateral uint64 `json:"collateral"`
}

// anchorSlot is the in-process mirror of one BPF map slot, plus the
// post-kill stats user-space accumulates.
type anchorSlot struct {
	armed      bool
	commandID  string
	anchor     []byte
	expected   [32]byte
	confirmed  uint64
	collateral uint64
}

// AnchorStore owns the BPF ``blocked_anchors`` map. Safe for concurrent
// use — RecordKill is called from the ringbuf consumer goroutine, Arm /
// Disarm from the downlink handler goroutine, Snapshot from the periodic
// reporter goroutine.
type AnchorStore struct {
	mp    *ebpf.Map
	mu    sync.Mutex
	slots [maxAnchors]anchorSlot
	// Atomic kill counter — bumped from the ringbuf hot path; the periodic
	// reporter reads it without taking the mutex.
	totalKills uint64
}

// NewAnchorStore wraps the BPF map handle. mp may be nil (test / loader
// returned no map) — every method becomes a no-op and Arm returns an error.
func NewAnchorStore(mp *ebpf.Map) *AnchorStore {
	return &AnchorStore{mp: mp}
}

// Arm writes an anchor + expected hash into the first empty slot and
// returns the slot index. Errors if every slot is occupied or the anchor
// is malformed (empty / too long).
func (s *AnchorStore) Arm(cmdID string, anchor []byte, expected [32]byte) (int, error) {
	if s.mp == nil {
		return -1, errors.New("anchors: BPF map unavailable")
	}
	if len(anchor) == 0 {
		return -1, errors.New("anchors: empty anchor")
	}
	if len(anchor) > anchorMax {
		return -1, fmt.Errorf("anchors: anchor too long (%d > %d)", len(anchor), anchorMax)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Find a free slot. If the same commandID is already armed, re-use
	// that slot (idempotent re-arm). Otherwise grab the first vacancy.
	slot := -1
	for i := range s.slots {
		if s.slots[i].armed && s.slots[i].commandID == cmdID {
			slot = i
			break
		}
	}
	if slot < 0 {
		for i := range s.slots {
			if !s.slots[i].armed {
				slot = i
				break
			}
		}
	}
	if slot < 0 {
		return -1, errors.New("anchors: all slots in use")
	}

	if err := writeSlotToMap(s.mp, slot, anchor); err != nil {
		return -1, fmt.Errorf("anchors: BPF update slot=%d: %w", slot, err)
	}

	s.slots[slot] = anchorSlot{
		armed:     true,
		commandID: cmdID,
		anchor:    append([]byte(nil), anchor...),
		expected:  expected,
	}
	return slot, nil
}

// Disarm clears the BPF map entry and the user-space mirror. Idempotent.
func (s *AnchorStore) Disarm(slot int) error {
	if s.mp == nil {
		return errors.New("anchors: BPF map unavailable")
	}
	if slot < 0 || slot >= maxAnchors {
		return fmt.Errorf("anchors: slot %d out of range", slot)
	}
	if err := writeSlotToMap(s.mp, slot, nil); err != nil {
		return fmt.Errorf("anchors: BPF clear slot=%d: %w", slot, err)
	}
	s.mu.Lock()
	s.slots[slot] = anchorSlot{}
	s.mu.Unlock()
	return nil
}

// DisarmByCommandID looks up the slot armed for the given command id and
// clears it. Returns the freed slot index, or -1 if no such slot.
func (s *AnchorStore) DisarmByCommandID(cmdID string) (int, error) {
	s.mu.Lock()
	slot := -1
	for i := range s.slots {
		if s.slots[i].armed && s.slots[i].commandID == cmdID {
			slot = i
			break
		}
	}
	s.mu.Unlock()
	if slot < 0 {
		return -1, nil
	}
	return slot, s.Disarm(slot)
}

// RecordKill is called for every EVT_SSL_KILL the agent receives. The
// caller passes the original SSL_write buffer (when available) so we can
// recompute the rolling hash and compare it to each armed anchor's
// expected hash. Returns the slot whose expected hash matched (or -1 if
// none did) and whether that match was the slot indicated by the BPF
// anchor scan (== "confirmed" kill).
//
// kernelSlot is the slot the BPF program reported as the trigger (the
// ev.AnchorSlot value, biased by -1 by the caller — pass -1 if the kill
// came from an SSL_ctx / PID hit instead of an anchor scan).
func (s *AnchorStore) RecordKill(buffer []byte, kernelSlot int) (matchedSlot int, confirmed bool) {
	atomic.AddUint64(&s.totalKills, 1)
	if len(buffer) == 0 {
		// Without the buffer we can't recompute a rolling hash. Charge
		// the BPF-reported slot as confirmed (we trust the kernel scan)
		// and move on. This is the fallback path; the main flow passes
		// the captured wire bytes.
		if kernelSlot >= 0 && kernelSlot < maxAnchors {
			s.mu.Lock()
			if s.slots[kernelSlot].armed {
				s.slots[kernelSlot].confirmed++
			}
			s.mu.Unlock()
			return kernelSlot, true
		}
		return -1, false
	}

	got, ok := bufferRollingHash(buffer)
	if !ok {
		// Couldn't parse → can't verify. Same fallback as above.
		if kernelSlot >= 0 && kernelSlot < maxAnchors {
			s.mu.Lock()
			if s.slots[kernelSlot].armed {
				s.slots[kernelSlot].confirmed++
			}
			s.mu.Unlock()
			return kernelSlot, true
		}
		return -1, false
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	matched := -1
	for i := range s.slots {
		if s.slots[i].armed && s.slots[i].expected == got {
			matched = i
			break
		}
	}
	if matched < 0 {
		// No armed anchor's hash matched the buffer. The kernel must
		// have struck via a different path (SSL_ctx, PID), or the
		// anchor that struck wasn't paired with this conversation.
		// Charge as collateral against the kernel-reported slot if any.
		if kernelSlot >= 0 && kernelSlot < maxAnchors && s.slots[kernelSlot].armed {
			s.slots[kernelSlot].collateral++
			return kernelSlot, false
		}
		return -1, false
	}
	if matched == kernelSlot {
		s.slots[matched].confirmed++
		return matched, true
	}
	// The hash matched a different anchor than the one the kernel
	// reported — likely two anchors armed for adjacent conversations.
	// Bias to "confirmed" since at least one armed entry matches the
	// observed buffer.
	s.slots[matched].confirmed++
	return matched, true
}

// MatchBody is the Layer-2 user-space substring scan. Walks the armed
// anchors and returns the first slot whose anchor bytes appear anywhere
// in ``body``. Returns slot index (or -1), the matching anchor bytes,
// and the slot's commandID. Used by the request-side handler to catch
// anchors that the kernel scan missed because the user message landed
// past the kernel SCAN_WINDOW (~512 B) — long system prompts or deep
// conversation chains push the content past the kernel's reach. The
// user-space scan sees the whole assembled HTTP body, so size doesn't
// matter.
func (s *AnchorStore) MatchBody(body []byte) (slot int, anchor []byte, cmdID string) {
	if len(body) == 0 {
		return -1, nil, ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.slots {
		if !s.slots[i].armed || len(s.slots[i].anchor) == 0 {
			continue
		}
		if bytesIndex(body, s.slots[i].anchor) >= 0 {
			anchorCopy := append([]byte(nil), s.slots[i].anchor...)
			return i, anchorCopy, s.slots[i].commandID
		}
	}
	return -1, nil, ""
}

// bytesIndex is a thin wrapper so we can swap in something fancier (e.g.
// Boyer-Moore) without touching callers. The Go stdlib's bytes.Index is
// already Rabin-Karp / SIMD-optimized on common architectures; this is
// fast enough for the per-request hot path (one call per captured body).
func bytesIndex(haystack, needle []byte) int {
	return _bytesIndex(haystack, needle)
}

// Snapshot returns a copy of the current per-slot stats.
func (s *AnchorStore) Snapshot() []SlotStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]SlotStats, 0, maxAnchors)
	for i := range s.slots {
		if !s.slots[i].armed {
			continue
		}
		out = append(out, SlotStats{
			Slot:       i,
			CommandID:  s.slots[i].commandID,
			AnchorLen:  len(s.slots[i].anchor),
			Confirmed:  s.slots[i].confirmed,
			Collateral: s.slots[i].collateral,
		})
	}
	return out
}

// TotalKills returns the cumulative kill count seen by the store.
func (s *AnchorStore) TotalKills() uint64 {
	return atomic.LoadUint64(&s.totalKills)
}

// writeSlotToMap encodes a struct anchor_entry and pushes it to the BPF
// array. Passing anchor=nil writes a zero-length (empty) entry, which the
// kernel treats as "slot disarmed".
func writeSlotToMap(mp *ebpf.Map, slot int, anchor []byte) error {
	buf := make([]byte, bpfAnchorEntrySize)
	binary.LittleEndian.PutUint32(buf[0:4], uint32(len(anchor)))
	copy(buf[4:], anchor)
	key := uint32(slot)
	return mp.Update(&key, buf, ebpf.UpdateAny)
}

// bufferRollingHash extracts the normalized message chain from an
// OpenAI/Anthropic request body and returns its rolling hash. Returns
// (zero, false) if the buffer isn't a recognizable request body. The
// fingerprint package can't be imported from policy (would create a
// cycle), so the caller is responsible for either supplying the parsed
// hash or wiring a small adapter — see cmd/agent/main.go's RecordKill
// caller.
//
// We keep this as a function variable so cmd/agent/main.go can inject
// the real parser at startup without a build-time import cycle.
var bufferRollingHash = func(_ []byte) ([32]byte, bool) {
	return [32]byte{}, false
}

// SetRollingHashFunc registers the function AnchorStore uses to derive a
// rolling hash from a captured SSL_write buffer. Called once at startup
// from cmd/agent/main.go with a closure that wires
// fingerprint.ExtractNormalizedMessages + fingerprint.ComputeRollingHashOver.
func SetRollingHashFunc(fn func([]byte) ([32]byte, bool)) {
	if fn != nil {
		bufferRollingHash = fn
	}
}
