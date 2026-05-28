// Package policy — PID-wide block window manager.
//
// When the operator kills a fingerprint we add the offending PIDs to the
// BPF `blocked_pids` map and remove them after a short TTL (default 200ms).
// Combined with the per-SSL_ctx armed map, this closes the race where the
// customer's TLS connection pool opens a new socket for the next turn —
// the new SSL_ctx isn't in blocked_ssl_contexts yet, but the PID is, so
// the very first write on the new socket gets corrupted.
//
// Collateral: during the TTL window, ALL writes from that PID are
// corrupted, including unrelated concurrent conversations. We keep the
// window tight (200ms default) so the blast radius stays small.

package policy

import (
	"log"
	"sync"
	"time"

	"github.com/cilium/ebpf"
)

// PIDWindow holds a short-TTL "block this PID entirely" set, backed by a
// BPF map that the kernel uprobe consults. User-space removes entries when
// their deadline passes.
type PIDWindow struct {
	mu       sync.Mutex
	mp       *ebpf.Map
	deadline map[uint32]time.Time
	defTTL   time.Duration
}

// New returns a PIDWindow bound to the given BPF map. ``defTTL`` is the
// default lifetime per Arm() call (200ms is a sensible default for
// catching httpx connection-pool churn without too much collateral).
// Pass a nil map when the loader didn't expose blocked_pids; Arm() becomes
// a no-op.
func NewPIDWindow(mp *ebpf.Map, defTTL time.Duration) *PIDWindow {
	if defTTL == 0 {
		defTTL = 200 * time.Millisecond
	}
	return &PIDWindow{
		mp:       mp,
		deadline: make(map[uint32]time.Time),
		defTTL:   defTTL,
	}
}

// Arm inserts the PID into the BPF map and schedules removal after the TTL.
// Idempotent — re-arming the same PID before its TTL expires extends the
// deadline.
func (p *PIDWindow) Arm(pid uint32) {
	p.ArmFor(pid, p.defTTL)
}

// ArmFor is the explicit variant.
func (p *PIDWindow) ArmFor(pid uint32, ttl time.Duration) {
	if p.mp == nil {
		return
	}
	val := uint8(1)
	if err := p.mp.Update(&pid, &val, ebpf.UpdateAny); err != nil {
		log.Printf("pidwindow: arm pid=%d failed: %v", pid, err)
		return
	}
	p.mu.Lock()
	p.deadline[pid] = time.Now().Add(ttl)
	p.mu.Unlock()
	log.Printf("pidwindow: armed pid=%d for %s", pid, ttl)
}

// Run sweeps expired entries every 25ms. Stops when ctx-channel closes.
func (p *PIDWindow) Run(stop <-chan struct{}) {
	if p.mp == nil {
		return
	}
	tick := time.NewTicker(25 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
			p.sweep()
		}
	}
}

func (p *PIDWindow) sweep() {
	now := time.Now()
	p.mu.Lock()
	expired := make([]uint32, 0)
	for pid, dl := range p.deadline {
		if now.After(dl) {
			expired = append(expired, pid)
		}
	}
	for _, pid := range expired {
		delete(p.deadline, pid)
	}
	p.mu.Unlock()
	for _, pid := range expired {
		if err := p.mp.Delete(&pid); err != nil {
			// Map might have evicted on its own; not fatal.
			continue
		}
		log.Printf("pidwindow: expired pid=%d", pid)
	}
}
