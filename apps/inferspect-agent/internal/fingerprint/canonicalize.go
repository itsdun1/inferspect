// Package fingerprint computes the conversation fingerprint used as the
// kill key. The construction (SHA256 over (system, first_user)) is
// intentionally minimal — see docs/Phase G plan section 4b for the rationale
// and the known revisit points. Anything that needs to change about the hash
// should change ONLY in this package.
package fingerprint

import (
	"encoding/json"
	"sort"
	"strings"
	"unicode"
)

// canonicalMessage is the stable shape we hash over. We don't trust the wire
// JSON's key order (OpenAI canonicalizes; Anthropic might not), so we marshal
// through this struct.
type canonicalMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// CanonicalizePrefix takes a list of normalized {role, content} pairs and
// returns the bytes we hash. Transformations applied:
//
//  1. Lowercase + strip the role.
//  2. Collapse internal whitespace runs to a single space.
//  3. Trim leading/trailing whitespace.
//  4. JSON-marshal with sorted keys + UTF-8 output.
//
// Phase G.1 deliberately skips Unicode normalization (NFKC) — see plan §4b
// "Known revisit points". Adding NFKC requires golang.org/x/text and is part
// of the canonicalization-rules iteration after capture is proven.
func CanonicalizePrefix(messages []canonicalMessage) ([]byte, error) {
	out := make([]canonicalMessage, 0, len(messages))
	for _, m := range messages {
		role := strings.ToLower(strings.TrimSpace(m.Role))
		content := normalizeContent(m.Content)
		if content == "" && role == "" {
			continue
		}
		out = append(out, canonicalMessage{Role: role, Content: content})
	}
	// Marshal with deterministic key order (Go's json.Marshal sorts struct
	// fields by declaration order, which matches our struct definition).
	// We use a small wrapper to make this explicit.
	return marshalCanonical(out)
}

func normalizeContent(s string) string {
	// Collapse whitespace runs (any unicode space → ' ').
	var b strings.Builder
	b.Grow(len(s))
	lastSpace := true
	for _, r := range s {
		if unicode.IsSpace(r) {
			if !lastSpace {
				b.WriteByte(' ')
				lastSpace = true
			}
			continue
		}
		b.WriteRune(r)
		lastSpace = false
	}
	return strings.TrimSpace(b.String())
}

// marshalCanonical serializes with stable key ordering. Since canonicalMessage
// is a fixed struct, json.Marshal already orders keys by field declaration —
// we just sort the slice deterministically (already in messages-order).
func marshalCanonical(msgs []canonicalMessage) ([]byte, error) {
	// We sort by role then content to make the hash independent of which
	// turn-relative position the messages happened to land in. NB: for the
	// system+first_user prefix this is a no-op since there's at most one of
	// each. Keeping it explicit makes future N>2 extensions safe.
	sort.SliceStable(msgs, func(i, j int) bool {
		if msgs[i].Role != msgs[j].Role {
			return roleRank(msgs[i].Role) < roleRank(msgs[j].Role)
		}
		return msgs[i].Content < msgs[j].Content
	})
	return json.Marshal(msgs)
}

func roleRank(r string) int {
	switch r {
	case "system":
		return 0
	case "user":
		return 1
	case "assistant":
		return 2
	default:
		return 3
	}
}
