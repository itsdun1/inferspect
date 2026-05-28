// Package uplink batches and ships observed events to ingestion-service's
// /v1/logs endpoint. Mirrors the SDK's BatchedLogTransport — same envelope
// shape, same idempotency semantics, same API key header.
//
// On POST failure the batch is persisted to a disk-backed queue and replayed
// by a background goroutine. See queue.go for the offline-buffer design.
package uplink

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Event is one event the agent wants to ship — a partially-filled SDK
// InferenceLog (or one of the other log types). The agent leaves the wire
// shape generic so we don't recompile when the SDK adds optional fields.
type Event map[string]any

type Transport struct {
	url      string
	apiKey   string
	service  string
	interval time.Duration
	maxBatch int
	http     *http.Client

	mu      sync.Mutex
	pending []Event

	flushCh chan struct{}

	queue *diskQueue
}

func New(rawURL, apiKey, service string, interval time.Duration, maxBatch int) *Transport {
	// rawURL is the base ingestion URL like https://chat.example.com/api.
	// We append /v1/logs for the SDK-shaped endpoint.
	url := strings.TrimRight(rawURL, "/") + "/v1/logs"
	httpClient := &http.Client{Timeout: 10 * time.Second}
	t := &Transport{
		url:      url,
		apiKey:   apiKey,
		service:  service,
		interval: interval,
		maxBatch: maxBatch,
		http:     httpClient,
		flushCh:  make(chan struct{}, 1),
	}

	queueDir := getenv("INFERSPECT_QUEUE_DIR", "/var/lib/inferspect/queue/")
	maxMB := getIntEnv("INFERSPECT_QUEUE_MAX_MB", 100)
	maxAgeH := getIntEnv("INFERSPECT_QUEUE_MAX_AGE_HOURS", 24)
	t.queue = newDiskQueue(
		queueDir,
		int64(maxMB)*1024*1024,
		time.Duration(maxAgeH)*time.Hour,
		url, apiKey, httpClient,
	)
	if err := t.queue.init(); err != nil {
		log.Printf("uplink: queue init failed (%v) — disk buffer disabled", err)
		t.queue = nil
	}
	return t
}

// Enqueue adds an event. Triggers a flush if we cross max_batch.
func (t *Transport) Enqueue(ev Event) {
	t.mu.Lock()
	t.pending = append(t.pending, ev)
	size := len(t.pending)
	t.mu.Unlock()
	if size >= t.maxBatch {
		select {
		case t.flushCh <- struct{}{}:
		default:
		}
	}
}

// Run blocks until ctx is done. Drives the periodic flush + max-batch flush
// + background drain of any disk-persisted batches.
func (t *Transport) Run(ctx context.Context) {
	if t.queue != nil {
		go t.queue.runDrain(ctx)
	}
	tick := time.NewTicker(t.interval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			t.flush(context.Background())
			return
		case <-tick.C:
			t.flush(ctx)
		case <-t.flushCh:
			t.flush(ctx)
		}
	}
}

func (t *Transport) flush(ctx context.Context) {
	t.mu.Lock()
	if len(t.pending) == 0 {
		t.mu.Unlock()
		return
	}
	batch := t.pending
	t.pending = nil
	t.mu.Unlock()

	envelope := map[string]any{
		"service":     t.service,
		"sdk_version": "ebpf-agent-0.1.0",
		"events":      batch,
	}
	body, err := json.Marshal(envelope)
	if err != nil {
		log.Printf("uplink: marshal failed: %v", err)
		return
	}

	if !t.deliver(ctx, body) {
		if t.queue != nil {
			if err := t.queue.persist(body); err != nil {
				log.Printf("uplink: queue persist failed (%d events dropped): %v", len(batch), err)
			} else {
				log.Printf("uplink: persisted batch of %d events to disk queue", len(batch))
			}
		} else {
			log.Printf("uplink: POST failed and queue disabled — %d events dropped", len(batch))
		}
	}
}

// deliver performs a single inline POST. Returns true on 2xx, false on any
// failure that the caller may want to retry via the disk queue. 4xx is
// treated as "delivered" (rejected payload — replay won't help).
func (t *Transport) deliver(ctx context.Context, body []byte) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.url, bytes.NewReader(body))
	if err != nil {
		log.Printf("uplink: build request failed: %v", err)
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	if t.apiKey != "" {
		req.Header.Set("X-Sdk-Key", t.apiKey)
	}
	resp, err := t.http.Do(req)
	if err != nil {
		log.Printf("uplink: POST failed: %v", err)
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true
	}
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		// Permanent rejection — don't enqueue to replay forever.
		log.Printf("uplink: HTTP %d (permanent, dropping)", resp.StatusCode)
		return true
	}
	log.Printf("uplink: HTTP %d (will retry via disk queue)", resp.StatusCode)
	return false
}

// Heartbeat sends a one-shot POST /v1/control/heartbeat with host metadata.
// Separate from the event flush so the registry shows the host before any
// observations have landed.
func (t *Transport) Heartbeat(ctx context.Context, hostID string, info map[string]any) error {
	base := strings.TrimRight(t.url, "/")
	// /v1/logs → /v1/control/heartbeat
	base = strings.TrimSuffix(base, "/logs") + "/control/heartbeat"
	body := map[string]any{"host_id": hostID}
	for k, v := range info {
		body[k] = v
	}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if t.apiKey != "" {
		req.Header.Set("X-Sdk-Key", t.apiKey)
	}
	resp, err := t.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("heartbeat http %d", resp.StatusCode)
	}
	return nil
}

func getenv(k, dflt string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return dflt
}

func getIntEnv(k string, dflt int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return dflt
}
