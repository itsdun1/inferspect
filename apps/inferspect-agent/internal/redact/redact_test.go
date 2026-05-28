package redact

import "testing"

func TestRedactText(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"contact jane.doe+test@example.co.uk soon", "contact <EMAIL> soon"},
		{"call (415) 555-1234", "call <PHONE>"},
		{"phone 415-555-1234 or +44 20 7946 0958", "phone <PHONE> or <PHONE>"},
		{"card 4111-1111-1111-1111", "card <CARD>"},
		{"ssn 123-45-6789", "ssn <SSN>"},
		{"from 192.168.1.42 last seen", "from <IP> last seen"},
		{"name three planets", "name three planets"},
		{"", ""},
	}
	for _, c := range cases {
		got := Text(c.in)
		if got != c.want {
			t.Errorf("Text(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestIdempotent(t *testing.T) {
	first := Text("contact a@b.com from 1.2.3.4")
	second := Text(first)
	if first != second {
		t.Errorf("not idempotent: %q -> %q", first, second)
	}
}
