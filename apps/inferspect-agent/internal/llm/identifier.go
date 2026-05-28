// Package llm identifies which LLM provider a captured HTTP request belongs to
// and extracts the model + messages from the body for fingerprinting.
package llm

import (
	"encoding/json"
	"strings"
)

type Provider string

const (
	ProviderOpenAI    Provider = "openai"
	ProviderAnthropic Provider = "anthropic"
	ProviderGoogle    Provider = "google"
	ProviderUnknown   Provider = ""
)

type Request struct {
	Provider Provider
	Model    string
	Body     []byte
	Stream   bool
}

// IdentifyByHost matches the Host header (or SNI fallback) against known
// provider domains.
func IdentifyByHost(host string) Provider {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" {
		return ProviderUnknown
	}
	switch {
	case strings.HasSuffix(host, "api.openai.com"):
		return ProviderOpenAI
	case strings.HasSuffix(host, "api.anthropic.com"):
		return ProviderAnthropic
	case strings.HasSuffix(host, "generativelanguage.googleapis.com"),
		strings.HasSuffix(host, "aiplatform.googleapis.com"):
		return ProviderGoogle
	}
	return ProviderUnknown
}

// ExtractModel reads the model name from the request body. Different providers
// use slightly different field locations but ``model`` at the top level is
// near-universal.
func ExtractModel(body []byte) string {
	var p struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(body, &p); err != nil {
		return ""
	}
	return p.Model
}

// ExtractStream returns true when the request is asking for streaming output.
func ExtractStream(body []byte) bool {
	var p struct {
		Stream bool `json:"stream"`
	}
	if err := json.Unmarshal(body, &p); err != nil {
		return false
	}
	return p.Stream
}
