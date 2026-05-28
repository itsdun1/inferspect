// Disk-backed offline queue for uplink batches.
//
// When a POST to ingestion fails (network error, 5xx, etc.) the Transport
// writes the JSON envelope to a file in ${INFERSPECT_QUEUE_DIR} (default
// /var/lib/inferspect/queue/). A background goroutine wakes every five
// seconds and replays the queued files in timestamp order. Each filename is
// prefixed with the unix-nano timestamp so a lexicographic sort matches
// chronological order.
//
// The queue is capped (default 100 MB / 24h). When a new batch would push
// over the size cap the OLDEST file is dropped — lossy by design; the
// alternative is dropping the freshest signal, which is worse.
package uplink

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// diskQueue persists JSON-encoded batches to disk and drains them in the
// background.
type diskQueue struct {
	dir       string
	maxBytes  int64
	maxAge    time.Duration
	url       string
	apiKey    string
	http      *http.Client

	mu sync.Mutex
}

func newDiskQueue(dir string, maxBytes int64, maxAge time.Duration, url, apiKey string, httpClient *http.Client) *diskQueue {
	return &diskQueue{
		dir:      dir,
		maxBytes: maxBytes,
		maxAge:   maxAge,
		url:      url,
		apiKey:   apiKey,
		http:     httpClient,
	}
}

// init prepares the queue directory and expires anything older than maxAge.
func (q *diskQueue) init() error {
	if err := os.MkdirAll(q.dir, 0o755); err != nil {
		return fmt.Errorf("queue mkdir %s: %w", q.dir, err)
	}
	q.expireOld()
	return nil
}

// persist writes a batch JSON payload to the queue. Enforces the size cap by
// evicting the oldest file(s) first when needed.
func (q *diskQueue) persist(payload []byte) error {
	q.mu.Lock()
	defer q.mu.Unlock()

	// Eviction loop: drop oldest files until we have headroom.
	files, total := q.snapshot()
	for total+int64(len(payload)) > q.maxBytes && len(files) > 0 {
		victim := files[0]
		if err := os.Remove(filepath.Join(q.dir, victim.name)); err == nil {
			log.Printf("queue: evicted oldest %s (%d bytes) to make room", victim.name, victim.size)
		}
		total -= victim.size
		files = files[1:]
	}

	rnd := make([]byte, 2)
	if _, err := rand.Read(rnd); err != nil {
		// Deterministic fallback — collisions are tolerable since the
		// nano timestamp varies.
		rnd = []byte{0, 0}
	}
	name := fmt.Sprintf("%d-%s.batch", time.Now().UnixNano(), hex.EncodeToString(rnd))
	tmp := filepath.Join(q.dir, name+".tmp")
	if err := os.WriteFile(tmp, payload, 0o644); err != nil {
		return err
	}
	final := filepath.Join(q.dir, name)
	return os.Rename(tmp, final)
}

// runDrain periodically attempts to flush the queue to the ingestion service.
// Blocks until ctx is cancelled.
func (q *diskQueue) runDrain(ctx context.Context) {
	tick := time.NewTicker(5 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			q.drainOnce(ctx)
		}
	}
}

func (q *diskQueue) drainOnce(ctx context.Context) {
	q.mu.Lock()
	files, _ := q.snapshot()
	q.mu.Unlock()
	if len(files) == 0 {
		return
	}
	sent := 0
	for _, f := range files {
		if ctx.Err() != nil {
			return
		}
		full := filepath.Join(q.dir, f.name)
		body, err := os.ReadFile(full)
		if err != nil {
			// Stale entry (race with eviction?) — try the next file.
			continue
		}
		if !q.post(ctx, body) {
			// Stop draining this tick; next tick will try again.
			remaining := len(files) - sent
			log.Printf("queue drain: %d batches sent, %d remaining", sent, remaining)
			return
		}
		if err := os.Remove(full); err != nil {
			log.Printf("queue: failed to remove %s: %v", f.name, err)
		}
		sent++
	}
	remaining := len(files) - sent
	if sent > 0 {
		log.Printf("queue drain: %d batches sent, %d remaining", sent, remaining)
	}
}

// post performs a single POST attempt. Returns true on 2xx success.
func (q *diskQueue) post(ctx context.Context, body []byte) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, q.url, bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	if q.apiKey != "" {
		req.Header.Set("X-Sdk-Key", q.apiKey)
	}
	resp, err := q.http.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true
	}
	// 4xx is a permanent error — the ingestion service won't accept this
	// payload no matter how many times we replay. Drop it so we don't keep
	// trying forever.
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		log.Printf("queue: dropping batch (server rejected with HTTP %d)", resp.StatusCode)
		return true
	}
	return false
}

type fileEntry struct {
	name    string
	size    int64
	modUnix int64
}

// snapshot returns the queue contents sorted by name (== timestamp order).
// Caller MUST hold q.mu.
func (q *diskQueue) snapshot() ([]fileEntry, int64) {
	entries, err := os.ReadDir(q.dir)
	if err != nil {
		return nil, 0
	}
	out := make([]fileEntry, 0, len(entries))
	var total int64
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".batch") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		out = append(out, fileEntry{
			name:    e.Name(),
			size:    info.Size(),
			modUnix: info.ModTime().Unix(),
		})
		total += info.Size()
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, total
}

// expireOld removes files older than q.maxAge based on the filename
// timestamp prefix (falls back to mtime if the name doesn't parse).
func (q *diskQueue) expireOld() {
	if q.maxAge <= 0 {
		return
	}
	cutoff := time.Now().Add(-q.maxAge).UnixNano()
	q.mu.Lock()
	files, _ := q.snapshot()
	q.mu.Unlock()
	for _, f := range files {
		ts := parseTimestamp(f.name)
		if ts == 0 {
			// Fall back to mtime.
			ts = f.modUnix * int64(time.Second/time.Nanosecond)
		}
		if ts < cutoff {
			path := filepath.Join(q.dir, f.name)
			if err := os.Remove(path); err == nil {
				log.Printf("queue: expired %s (age > %s)", f.name, q.maxAge)
			}
		}
	}
}

func parseTimestamp(name string) int64 {
	// Filename: <unix_nano>-<hex>.batch
	dash := strings.IndexByte(name, '-')
	if dash <= 0 {
		return 0
	}
	v, err := strconv.ParseInt(name[:dash], 10, 64)
	if err != nil {
		return 0
	}
	return v
}
