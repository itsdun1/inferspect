// Package config holds the agent's runtime configuration, loaded from
// environment variables. Defaults are tuned for the local docker-compose dev
// loop — production deployments override via the systemd unit / DaemonSet env.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	// HostID is the stable identifier for this host. Used in event payloads
	// and as the routing key for the control channel. Loaded from
	// /var/lib/inferspect/host_id when set, otherwise derived from the
	// container's hostname.
	HostID string

	// IngestionURL is the base URL of the ingestion service, e.g.
	// https://chat.example.com/api. The agent appends /logs for uplink and
	// /control/{poll,kill,heartbeat} for downlink.
	IngestionURL string

	// APIKey is the SDK key the agent sends in X-Sdk-Key.
	APIKey string

	// LibSSLPaths is a colon-separated list of libssl shared objects to
	// attach uprobes to. The agent attempts each in order and attaches to
	// the first one that exists.
	LibSSLPaths []string

	// BatchInterval flushes accumulated events on a fixed cadence.
	BatchInterval time.Duration

	// BatchMaxEvents flushes early when the in-memory buffer hits this size.
	BatchMaxEvents int

	// PollTimeout is the long-poll hold time.
	PollTimeout time.Duration

	// ServiceName is the value stamped on outbound events as ``service``.
	ServiceName string

	// AgentVersion reported via heartbeat.
	AgentVersion string
}

func Load() (*Config, error) {
	cfg := &Config{
		IngestionURL:   getenv("INFERSPECT_INGESTION_URL", "http://ingestion-service:8001"),
		APIKey:         os.Getenv("INFERSPECT_API_KEY"),
		BatchInterval:  getDurationEnv("INFERSPECT_BATCH_INTERVAL", 250*time.Millisecond),
		BatchMaxEvents: getIntEnv("INFERSPECT_BATCH_MAX_EVENTS", 256),
		PollTimeout:    getDurationEnv("INFERSPECT_POLL_TIMEOUT", 60*time.Second),
		ServiceName:    getenv("INFERSPECT_SERVICE", "inferspect-agent"),
		AgentVersion:   getenv("INFERSPECT_AGENT_VERSION", "0.1.0"),
	}

	libssl := getenv("INFERSPECT_LIBSSL_PATHS", strings.Join(defaultLibsslPaths(), ":"))
	for _, p := range strings.Split(libssl, ":") {
		p = strings.TrimSpace(p)
		if p != "" {
			cfg.LibSSLPaths = append(cfg.LibSSLPaths, p)
		}
	}

	hostIDFile := getenv("INFERSPECT_HOST_ID_FILE", "/var/lib/inferspect/host_id")
	cfg.HostID = readHostID(hostIDFile)

	if cfg.APIKey == "" {
		return nil, fmt.Errorf("INFERSPECT_API_KEY is required")
	}
	if cfg.HostID == "" {
		return nil, fmt.Errorf("could not determine host_id (set INFERSPECT_HOST_ID or write %s)", hostIDFile)
	}
	return cfg, nil
}

func defaultLibsslPaths() []string {
	// In order of preference. The agent attaches to the first one that
	// exists at runtime. Both common x86_64 and aarch64 multiarch paths
	// covered — production fleets are mixed.
	return []string{
		"/usr/lib/x86_64-linux-gnu/libssl.so.3",
		"/usr/lib/x86_64-linux-gnu/libssl.so.1.1",
		"/usr/lib/aarch64-linux-gnu/libssl.so.3",
		"/usr/lib/aarch64-linux-gnu/libssl.so.1.1",
		"/usr/lib64/libssl.so.3",
		"/usr/lib/libssl.so.3",
		"/lib/x86_64-linux-gnu/libssl.so.3",
		"/lib/aarch64-linux-gnu/libssl.so.3",
	}
}

func readHostID(path string) string {
	if v := os.Getenv("INFERSPECT_HOST_ID"); v != "" {
		return v
	}
	if b, err := os.ReadFile(path); err == nil {
		if s := strings.TrimSpace(string(b)); s != "" {
			return s
		}
	}
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return ""
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

func getDurationEnv(k string, dflt time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return dflt
}
