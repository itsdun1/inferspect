package downlink

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Handler is what the agent passes to Run — invoked once per command in
// arrival order. Returning an error logs it; the poller keeps going.
type Handler func(ctx context.Context, cmd Command) error

type Poller struct {
	baseURL    string
	apiKey     string
	hostID     string
	timeoutS   int
	httpClient *http.Client

	cursor string
}

// New builds a poller. ``baseURL`` is the ingestion base, e.g. https://chat.example.com/api.
// The poller appends /v1/control/poll itself.
func New(baseURL, apiKey, hostID string, timeout time.Duration) *Poller {
	return &Poller{
		baseURL:  strings.TrimRight(baseURL, "/"),
		apiKey:   apiKey,
		hostID:   hostID,
		timeoutS: int(timeout.Seconds()),
		httpClient: &http.Client{
			// Server holds for up to ``timeout`` seconds, so we have to
			// be patient on this side.
			Timeout: timeout + 30*time.Second,
		},
	}
}

// Run blocks until ctx is cancelled. Exponential backoff (1, 2, 4, ..., 30s)
// on transport errors; resets to 0 on a successful poll.
func (p *Poller) Run(ctx context.Context, h Handler) {
	backoff := time.Second
	for {
		if ctx.Err() != nil {
			return
		}
		commands, cursor, err := p.pollOnce(ctx)
		if err != nil {
			log.Printf("downlink: poll failed: %v (backoff %s)", err, backoff)
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff < 30*time.Second {
				backoff *= 2
				if backoff > 30*time.Second {
					backoff = 30 * time.Second
				}
			}
			continue
		}
		backoff = time.Second
		p.cursor = cursor
		for _, cmd := range commands {
			if err := h(ctx, cmd); err != nil {
				log.Printf("downlink: handler error for %s: %v", cmd.Command, err)
			}
		}
	}
}

func (p *Poller) pollOnce(ctx context.Context) ([]Command, string, error) {
	q := url.Values{}
	q.Set("host_id", p.hostID)
	if p.cursor != "" {
		q.Set("cursor", p.cursor)
	}
	q.Set("timeout", fmt.Sprintf("%d", p.timeoutS))

	endpoint := fmt.Sprintf("%s/v1/control/poll?%s", p.baseURL, q.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, "", err
	}
	if p.apiKey != "" {
		req.Header.Set("X-Sdk-Key", p.apiKey)
	}
	resp, err := p.httpClient.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil, "", fmt.Errorf("http %d", resp.StatusCode)
	}
	var out PollResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, "", err
	}
	return out.Commands, out.Cursor, nil
}
