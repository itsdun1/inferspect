package fingerprint

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
)

// ErrInsufficientPrefix is returned when the request body lacks both a
// system message and a user message to hash over. Callers should pass the
// event through unblockable.
var ErrInsufficientPrefix = errors.New("fingerprint: no (system, first_user) pair available")

// FromOpenAIBody computes the conversation fingerprint from an OpenAI-style
// chat completion request body. Returns the 64-character hex SHA256 of the
// canonicalized (system, first_user) prefix.
//
// Wire shape:
//
//	{"model":"gpt-4o","messages":[{"role":"system",...},{"role":"user",...},...]}
func FromOpenAIBody(body []byte) (string, error) {
	var parsed struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", err
	}
	return fromMessages(parsed.Messages)
}

// FromAnthropicBody is the same idea but Anthropic puts the system prompt at
// the top level (not inside ``messages``).
//
//	{"model":"claude-haiku-...","system":"...","messages":[{"role":"user",...},...]}
func FromAnthropicBody(body []byte) (string, error) {
	var parsed struct {
		System   string `json:"system"`
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", err
	}
	// Reshape into the (system, user) prefix we hash.
	msgs := make([]struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}, 0, 1+len(parsed.Messages))
	if parsed.System != "" {
		msgs = append(msgs, struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		}{
			Role:    "system",
			Content: json.RawMessage(quote(parsed.System)),
		})
	}
	msgs = append(msgs, parsed.Messages...)
	return fromMessages(msgs)
}

func fromMessages(msgs []struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}) (string, error) {
	prefix := make([]canonicalMessage, 0, 2)
	for _, m := range msgs {
		if m.Role == "system" {
			prefix = append(prefix, canonicalMessage{
				Role:    "system",
				Content: extractContent(m.Content),
			})
			break
		}
	}
	for _, m := range msgs {
		if m.Role == "user" {
			prefix = append(prefix, canonicalMessage{
				Role:    "user",
				Content: extractContent(m.Content),
			})
			break
		}
	}
	if len(prefix) == 0 {
		return "", ErrInsufficientPrefix
	}
	bytes, err := CanonicalizePrefix(prefix)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(bytes)
	return hex.EncodeToString(sum[:]), nil
}

// ExtractNormalizedMessages walks an OpenAI- or Anthropic-style request body
// and returns the message chain in canonical form (lowercase role,
// whitespace-collapsed content). This is the input the Tracker hashes over.
// For Anthropic, the top-level ``system`` field is synthesized into the
// chain as a leading {"role":"system",...} message.
func ExtractNormalizedMessages(body []byte) ([]NormalizedMessage, error) {
	// Try OpenAI shape first.
	var openai struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(body, &openai); err == nil && len(openai.Messages) > 0 {
		out := make([]NormalizedMessage, 0, len(openai.Messages))
		for _, m := range openai.Messages {
			out = append(out, NormalizedMessage{
				Role:    normalizeRole(m.Role),
				Content: normalizeContent(extractContent(m.Content)),
			})
		}
		return out, nil
	}

	// Try Anthropic shape (system at top level).
	var anthropic struct {
		System   string `json:"system"`
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(body, &anthropic); err != nil {
		return nil, err
	}
	out := make([]NormalizedMessage, 0, 1+len(anthropic.Messages))
	if anthropic.System != "" {
		out = append(out, NormalizedMessage{
			Role:    "system",
			Content: normalizeContent(anthropic.System),
		})
	}
	for _, m := range anthropic.Messages {
		out = append(out, NormalizedMessage{
			Role:    normalizeRole(m.Role),
			Content: normalizeContent(extractContent(m.Content)),
		})
	}
	if len(out) == 0 {
		return nil, ErrInsufficientPrefix
	}
	return out, nil
}

func normalizeRole(r string) string {
	// canonicalize.go's normalizeContent collapses whitespace; for roles we
	// just lowercase + trim, which keeps "system"/"user"/"assistant" intact.
	out := make([]byte, 0, len(r))
	for _, c := range []byte(r) {
		if c == ' ' || c == '\t' || c == '\n' || c == '\r' {
			continue
		}
		if c >= 'A' && c <= 'Z' {
			c = c + ('a' - 'A')
		}
		out = append(out, c)
	}
	return string(out)
}

// extractContent handles both string content ("hi there") and the structured
// list form ([{"type":"text","text":"hi"},...]). Returns concatenated text.
func extractContent(raw json.RawMessage) string {
	// String case.
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	// List of {type,text} blocks.
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &blocks); err == nil {
		out := make([]byte, 0, 64)
		for _, b := range blocks {
			if b.Type == "text" {
				out = append(out, []byte(b.Text)...)
			}
		}
		return string(out)
	}
	// Fallback — return the raw JSON. Stable for hashing if the producer is
	// consistent.
	return string(raw)
}

func quote(s string) []byte {
	b, _ := json.Marshal(s)
	return b
}
