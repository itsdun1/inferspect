// Package policy is the bridge between the control-channel commands and the
// in-kernel BPF maps. For Phase G.1 we keep the blocked-fingerprint set in
// user-space and translate "block this fingerprint" into "block this socket"
// at the point where we observe a request with that fingerprint.
//
// The kernel-side ``blocked_sockets`` map is the on-the-wire enforcement
// surface; this package owns writes to it.
package policy

import (
	"sync"
	"time"
)

// Store holds the operator-issued blocked fingerprints. Pure user-space.
// Lookups happen on every captured request (cheap — hash map by fingerprint).
type Store struct {
	mu        sync.RWMutex
	blocked   map[string]time.Time // fingerprint → expiry
	gcEvery   time.Duration
	lastSweep time.Time
}

func NewStore() *Store {
	return &Store{
		blocked: make(map[string]time.Time),
		gcEvery: 60 * time.Second,
	}
}

// Block adds a fingerprint with a TTL. ttlSeconds <= 0 → 1h default.
func (s *Store) Block(fingerprint string, ttlSeconds int) {
	if fingerprint == "" {
		return
	}
	if ttlSeconds <= 0 {
		ttlSeconds = 3600
	}
	s.mu.Lock()
	s.blocked[fingerprint] = time.Now().Add(time.Duration(ttlSeconds) * time.Second)
	s.maybeSweepLocked()
	s.mu.Unlock()
}

func (s *Store) Unblock(fingerprint string) {
	s.mu.Lock()
	delete(s.blocked, fingerprint)
	s.mu.Unlock()
}

// IsBlocked reports whether the fingerprint is currently in the deny set.
func (s *Store) IsBlocked(fingerprint string) bool {
	if fingerprint == "" {
		return false
	}
	s.mu.RLock()
	expiry, ok := s.blocked[fingerprint]
	s.mu.RUnlock()
	if !ok {
		return false
	}
	return time.Now().Before(expiry)
}

func (s *Store) maybeSweepLocked() {
	now := time.Now()
	if now.Sub(s.lastSweep) < s.gcEvery {
		return
	}
	s.lastSweep = now
	for fp, expiry := range s.blocked {
		if now.After(expiry) {
			delete(s.blocked, fp)
		}
	}
}
