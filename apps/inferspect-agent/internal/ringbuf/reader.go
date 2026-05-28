// Package ringbuf wraps the raw cilium/ebpf reader and decodes the C struct
// shape declared in bpf/ssl_uprobe.c.
package ringbuf

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"

	cilringbuf "github.com/cilium/ebpf/ringbuf"
)

// Event matches the C struct ``ssl_event`` in bpf/ssl_uprobe.c. Keep field
// order + sizes identical; any change requires recompiling the .o.
type Event struct {
	Type      uint8
	Truncated uint8
	// AnchorSlot is the BPF anchor-slot index + 1 that caused a kill, or 0
	// when the kill came from an SSL_ctx / PID hit (not an anchor scan).
	// Field is overlaid on the C struct's ``_pad`` slot — see
	// bpf/ssl_uprobe.c#enforce_if_blocked.
	AnchorSlot uint16
	PID        uint32
	TID        uint32
	TSNs       uint64
	SSLCtx     uint64
	Len        uint32
	Total      uint32
	Payload    []byte // sliced from the raw record below
}

const (
	EvtSSLWrite uint8 = 1
	EvtSSLRead  uint8 = 2
	// EvtSSLKill is emitted by the BPF program when in-kernel enforcement
	// fired on a write. Carries no payload — just (ssl_ctx, pid, total).
	EvtSSLKill uint8 = 3

	maxPayload = 8192
	// Header layout matches bpf/ssl_uprobe.c's struct ssl_event:
	//   ts_ns(8) ssl_ctx(8) pid(4) tid(4) len(4) total(4) type(1) trunc(1) pad(2) = 36
	headerSize = 36
)

// Reader produces decoded events from the BPF ringbuf.
type Reader struct {
	r *cilringbuf.Reader
}

func New(r *cilringbuf.Reader) *Reader {
	return &Reader{r: r}
}

// Run blocks until ctx is cancelled, invoking ``onEvent`` for each record.
func (rr *Reader) Run(ctx context.Context, onEvent func(Event)) error {
	for {
		if ctx.Err() != nil {
			return nil
		}
		record, err := rr.r.Read()
		if err != nil {
			if errors.Is(err, cilringbuf.ErrClosed) {
				return nil
			}
			return fmt.Errorf("ringbuf read: %w", err)
		}
		ev, ok := decode(record.RawSample)
		if !ok {
			continue
		}
		onEvent(ev)
	}
}

func decode(b []byte) (Event, bool) {
	if len(b) < headerSize {
		return Event{}, false
	}
	ev := Event{
		TSNs:       binary.LittleEndian.Uint64(b[0:8]),
		SSLCtx:     binary.LittleEndian.Uint64(b[8:16]),
		PID:        binary.LittleEndian.Uint32(b[16:20]),
		TID:        binary.LittleEndian.Uint32(b[20:24]),
		Len:        binary.LittleEndian.Uint32(b[24:28]),
		Total:      binary.LittleEndian.Uint32(b[28:32]),
		Type:       b[32],
		Truncated:  b[33],
		AnchorSlot: binary.LittleEndian.Uint16(b[34:36]),
	}
	// Payload follows the header. Trust ev.Len up to MAX_PAYLOAD.
	pl := int(ev.Len)
	if pl > maxPayload {
		pl = maxPayload
	}
	if headerSize+pl > len(b) {
		pl = len(b) - headerSize
	}
	if pl < 0 {
		pl = 0
	}
	ev.Payload = make([]byte, pl)
	copy(ev.Payload, b[headerSize:headerSize+pl])
	return ev, true
}
