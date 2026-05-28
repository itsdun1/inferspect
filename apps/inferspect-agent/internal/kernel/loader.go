// Package kernel handles BPF object loading and uprobe attachment.
//
// Phase G.1 keeps the kernel side minimal: capture-only via ringbuf. The
// blocked_sockets map is declared in the C program so we can wire enforcement
// in a follow-up without recompiling the .o.
package kernel

import (
	"errors"
	"fmt"
	"log"
	"os"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/ringbuf"
	"github.com/cilium/ebpf/rlimit"
)

// Loader manages the lifecycle of the BPF objects.
type Loader struct {
	coll       *ebpf.Collection
	uprobes    []link.Link
	ringReader *ringbuf.Reader
	libsslPath string
}

// Load reads the compiled ssl_uprobe.o, mounts the ringbuf, and attaches the
// uprobes against the first libssl found in the provided candidate paths.
func Load(objectPath string, libsslCandidates []string) (*Loader, error) {
	// On kernels with BPF cgroup-based memory accounting (5.11+) this is a
	// no-op. On Docker Desktop's stripped Linux VM the cgroup probe returns
	// ENOSYS — we log and continue; if the verifier later complains about
	// memlock, we'll see a clearer error there.
	if err := rlimit.RemoveMemlock(); err != nil {
		log.Printf("kernel: rlimit.RemoveMemlock non-fatal: %v", err)
	}

	spec, err := ebpf.LoadCollectionSpec(objectPath)
	if err != nil {
		return nil, fmt.Errorf("load spec %s: %w", objectPath, err)
	}
	coll, err := ebpf.NewCollection(spec)
	if err != nil {
		return nil, fmt.Errorf("new collection: %w", err)
	}

	libsslPath := firstExisting(libsslCandidates)
	if libsslPath == "" {
		coll.Close()
		return nil, errors.New("no libssl found in candidate paths")
	}

	ex, err := link.OpenExecutable(libsslPath)
	if err != nil {
		coll.Close()
		return nil, fmt.Errorf("open libssl %s: %w", libsslPath, err)
	}

	var attached []link.Link
	attach := func(symbol, prog string, isRet bool) error {
		p, ok := coll.Programs[prog]
		if !ok {
			return fmt.Errorf("program %s not found", prog)
		}
		var l link.Link
		var err error
		if isRet {
			l, err = ex.Uretprobe(symbol, p, nil)
		} else {
			l, err = ex.Uprobe(symbol, p, nil)
		}
		if err != nil {
			return fmt.Errorf("attach %s on %s: %w", prog, symbol, err)
		}
		attached = append(attached, l)
		return nil
	}

	// Attach to BOTH classic and _ex variants. OpenSSL 3 clients (Python's
	// _ssl module, httpx, recent curl) call the _ex versions; older code
	// paths call the classic ones. Hooking both keeps coverage broad.
	// We tolerate "symbol not found" so older libssl 1.1 hosts (no _ex)
	// still load — only the missing-symbol attaches are skipped.
	mustAttach := []struct{ sym, prog string; ret bool }{
		{"SSL_write", "uprobe_SSL_write", false},
		{"SSL_read", "uprobe_SSL_read_entry", false},
		{"SSL_read", "uretprobe_SSL_read", true},
	}
	optionalAttach := []struct{ sym, prog string; ret bool }{
		{"SSL_write_ex", "uprobe_SSL_write_ex", false},
		{"SSL_read_ex", "uprobe_SSL_read_ex_entry", false},
		{"SSL_read_ex", "uretprobe_SSL_read_ex", true},
	}
	for _, p := range mustAttach {
		if err := attach(p.sym, p.prog, p.ret); err != nil {
			closeAll(attached)
			coll.Close()
			return nil, err
		}
	}
	for _, p := range optionalAttach {
		if err := attach(p.sym, p.prog, p.ret); err != nil {
			log.Printf("kernel: optional uprobe %s on %s skipped: %v", p.prog, p.sym, err)
		}
	}

	eventsMap, ok := coll.Maps["events"]
	if !ok {
		closeAll(attached)
		coll.Close()
		return nil, errors.New("events map not found in BPF object")
	}
	rb, err := ringbuf.NewReader(eventsMap)
	if err != nil {
		closeAll(attached)
		coll.Close()
		return nil, fmt.Errorf("open ringbuf: %w", err)
	}

	return &Loader{
		coll:       coll,
		uprobes:    attached,
		ringReader: rb,
		libsslPath: libsslPath,
	}, nil
}

// LibsslPath returns the resolved path the loader attached to.
func (l *Loader) LibsslPath() string { return l.libsslPath }

// Ringbuf returns the reader so the agent can consume events.
func (l *Loader) Ringbuf() *ringbuf.Reader { return l.ringReader }

// BlockedSockets returns the BPF map used for in-kernel socket enforcement.
// Returns nil if the map isn't defined in the loaded object (older builds).
func (l *Loader) BlockedSockets() *ebpf.Map {
	return l.coll.Maps["blocked_sockets"]
}

// BlockedSSLContexts returns the BPF map of SSL ctx pointers the kernel
// should disrupt on the next SSL_write entry. User-space populates this
// when a fingerprint hits the operator deny set.
func (l *Loader) BlockedSSLContexts() *ebpf.Map {
	return l.coll.Maps["blocked_ssl_contexts"]
}

// BlockedPIDs returns the BPF map of PIDs the kernel should disrupt for a
// short TTL window after a fingerprint kill. Catches TLS pool churn where
// the next turn uses a brand-new SSL_ctx the agent hasn't seen yet.
func (l *Loader) BlockedPIDs() *ebpf.Map {
	return l.coll.Maps["blocked_pids"]
}

// BlockedAnchors returns the BPF array map of content-anchor patterns. The
// kernel scans the head of every SSL_write for a substring match against
// any armed anchor. Owned by internal/policy.AnchorStore. Phase G.4.
func (l *Loader) BlockedAnchors() *ebpf.Map {
	return l.coll.Maps["blocked_anchors"]
}

// Close releases all resources.
func (l *Loader) Close() {
	if l.ringReader != nil {
		_ = l.ringReader.Close()
	}
	closeAll(l.uprobes)
	if l.coll != nil {
		l.coll.Close()
	}
}

func closeAll(ls []link.Link) {
	for _, l := range ls {
		_ = l.Close()
	}
}

func firstExisting(paths []string) string {
	for _, p := range paths {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}
