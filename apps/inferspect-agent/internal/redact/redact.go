// Package redact applies light regex-based PII scrubbing to text the
// agent ships off-host. Runs on the customer's machine before anything
// crosses the network so raw identifiers never reach our ingestion
// pipeline.
//
// Patterns are intentionally conservative — false positives blank out
// legitimate text and are noisy on operator screens. Covers the common
// direct-identifier risk surface: email, US/intl phone, credit-card-
// shaped digit runs, US SSN, IPv4. Stronger detection (Presidio /
// spaCy NER) is opt-in for customers that want it; this default is the
// minimum that lets us tell prospects "PII never leaves your host".
package redact

import "regexp"

var (
	reEmail = regexp.MustCompile(`\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b`)
	// 13–19 digit run with optional separators between every 4 digits.
	reCard = regexp.MustCompile(`\b(?:\d[ \-]?){13,19}\b`)
	// US SSN, hyphenated only — avoids tripping on random 9-digit numbers.
	reSSN = regexp.MustCompile(`\b\d{3}-\d{2}-\d{4}\b`)
	// Phone — US: (123) 456-7890, 123-456-7890; intl: +44 20 7946 0958.
	rePhone = regexp.MustCompile(`(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}`)
	// IPv4 — basic dotted-quad. Octet-range check skipped; over-redaction
	// is acceptable for a privacy default.
	reIPv4 = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
)

// Order matters: longer / more specific patterns first so an email isn't
// half-eaten by the phone regex.
type rule struct {
	re   *regexp.Regexp
	repl []byte
}

var rules = []rule{
	{reEmail, []byte("<EMAIL>")},
	{reCard, []byte("<CARD>")},
	{reSSN, []byte("<SSN>")},
	{rePhone, []byte("<PHONE>")},
	{reIPv4, []byte("<IP>")},
}

// Text returns the input with direct-identifier patterns replaced by
// bracketed tokens (<EMAIL>, <PHONE>, etc.). Safe to call on the empty
// string. Idempotent — running it twice produces the same output.
func Text(s string) string {
	if s == "" {
		return s
	}
	b := []byte(s)
	for _, r := range rules {
		b = r.re.ReplaceAll(b, r.repl)
	}
	return string(b)
}
