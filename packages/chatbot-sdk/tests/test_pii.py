"""Tests for the pure-regex PII redactor in ``chatbot_sdk.pii``."""

from __future__ import annotations

import pytest

from chatbot_sdk.pii import DEFAULT_RECOGNIZERS, redact_dict, redact_text


def test_email_is_redacted():
    out = redact_text("contact me at alice@example.com please")
    assert "alice@example.com" not in out
    assert "<EMAIL_ADDRESS>" in out


def test_email_with_plus_and_dots():
    out = redact_text("send to first.last+tag@sub.example.co.uk")
    assert "<EMAIL_ADDRESS>" in out
    assert "first.last+tag" not in out


def test_phone_e164():
    out = redact_text("Call +14155552671 now")
    assert "<PHONE_NUMBER>" in out
    assert "+14155552671" not in out


def test_phone_north_american_with_dashes():
    out = redact_text("ring 415-555-2671 today")
    assert "<PHONE_NUMBER>" in out
    assert "415-555-2671" not in out


def test_phone_north_american_with_dots():
    out = redact_text("ring 415.555.2671 today")
    assert "<PHONE_NUMBER>" in out


def test_phone_north_american_with_spaces():
    out = redact_text("ring 415 555 2671 today")
    assert "<PHONE_NUMBER>" in out


def test_credit_card_valid_luhn_visa():
    # Standard Visa test card — passes Luhn.
    out = redact_text("Charge 4111 1111 1111 1111 to my account")
    assert "<CREDIT_CARD>" in out
    assert "4111" not in out


def test_credit_card_valid_luhn_amex():
    # Standard Amex test card.
    out = redact_text("Card 378282246310005")
    assert "<CREDIT_CARD>" in out


def test_credit_card_invalid_luhn_not_redacted():
    # 16 digits but doesn't pass Luhn — leave alone.
    out = redact_text("Order id 1234567890123456 placed")
    assert "<CREDIT_CARD>" not in out


def test_ssn_redacted():
    out = redact_text("SSN 123-45-6789 confirmed")
    assert "<US_SSN>" in out
    assert "123-45-6789" not in out


def test_iban_redacted():
    # UK IBAN (22 chars).
    out = redact_text("Send to GB82WEST12345698765432 from Lloyds")
    assert "<IBAN_CODE>" in out
    assert "GB82WEST12345698765432" not in out


def test_iban_too_short_not_redacted():
    out = redact_text("Code GB82WE not valid")
    assert "<IBAN_CODE>" not in out


def test_ipv4_redacted():
    out = redact_text("Origin 192.168.1.100 hit the API")
    assert "<IPV4>" in out
    assert "192.168.1.100" not in out


def test_ipv4_invalid_octet_not_redacted():
    out = redact_text("version 1.2.999.4 release")
    assert "<IPV4>" not in out


def test_aws_access_key_redacted():
    out = redact_text("token AKIAIOSFODNN7EXAMPLE leaked")
    assert "<AWS_ACCESS_KEY>" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_ipv6_not_in_defaults():
    # Explicitly opt-out by default — confirm a plausible IPv6 string is left
    # alone unless the caller asks for it.
    text = "host 2001:db8::1 reachable"
    out_default = redact_text(text)
    assert "<IPV6>" not in out_default

    out_opt_in = redact_text(text, recognizers=DEFAULT_RECOGNIZERS + ["IPV6"])
    assert "<IPV6>" in out_opt_in


def test_combined_pii_in_one_string():
    text = (
        "User alice@example.com from 10.0.0.1 paid with 4111111111111111 "
        "phone +14155552671 SSN 123-45-6789"
    )
    out = redact_text(text)
    for token in ("alice@example.com", "10.0.0.1", "4111111111111111",
                  "+14155552671", "123-45-6789"):
        assert token not in out
    for tag in ("<EMAIL_ADDRESS>", "<IPV4>", "<CREDIT_CARD>",
                "<PHONE_NUMBER>", "<US_SSN>"):
        assert tag in out


def test_redact_text_handles_empty():
    assert redact_text("") == ""


def test_redact_text_non_string_returned_as_is():
    # The function is typed for str but should be defensive.
    assert redact_text(None) is None  # type: ignore[arg-type]


def test_redact_text_with_subset_recognizers():
    text = "alice@example.com from 10.0.0.1"
    out = redact_text(text, recognizers=["EMAIL_ADDRESS"])
    assert "<EMAIL_ADDRESS>" in out
    assert "10.0.0.1" in out  # IPv4 not in the subset


def test_redact_text_no_pii_unchanged():
    text = "Just a friendly greeting."
    assert redact_text(text) == text


def test_redact_dict_recurses_into_nested_structures():
    payload = {
        "user": {
            "email": "alice@example.com",
            "ssn": "123-45-6789",
            "phones": ["+14155552671", "555-123-4567", "not-a-number"],
        },
        "ip": "10.0.0.1",
        "note": "plain text",
        "metadata": {
            "items": [
                {"contact": "bob@example.com"},
                {"contact": "still safe"},
            ],
        },
    }
    out = redact_dict(payload)
    assert out["user"]["email"] == "<EMAIL_ADDRESS>"
    assert out["user"]["ssn"] == "<US_SSN>"
    assert out["user"]["phones"][0] == "<PHONE_NUMBER>"
    assert out["user"]["phones"][1] == "<PHONE_NUMBER>"
    assert out["user"]["phones"][2] == "not-a-number"
    assert out["ip"] == "<IPV4>"
    assert out["note"] == "plain text"
    assert out["metadata"]["items"][0]["contact"] == "<EMAIL_ADDRESS>"
    assert out["metadata"]["items"][1]["contact"] == "still safe"


def test_redact_dict_preserves_non_strings():
    payload = {"count": 7, "ok": True, "email": "alice@example.com", "x": None}
    out = redact_dict(payload)
    assert out["count"] == 7
    assert out["ok"] is True
    assert out["x"] is None
    assert out["email"] == "<EMAIL_ADDRESS>"


@pytest.mark.parametrize(
    "name",
    DEFAULT_RECOGNIZERS,
)
def test_each_default_recognizer_has_a_sample(name):
    samples = {
        "EMAIL_ADDRESS": ("a@b.co", "<EMAIL_ADDRESS>"),
        "PHONE_NUMBER": ("+14155552671", "<PHONE_NUMBER>"),
        "CREDIT_CARD": ("4111111111111111", "<CREDIT_CARD>"),
        "US_SSN": ("123-45-6789", "<US_SSN>"),
        "IBAN_CODE": ("GB82WEST12345698765432", "<IBAN_CODE>"),
        "IPV4": ("10.0.0.1", "<IPV4>"),
        "AWS_ACCESS_KEY": ("AKIAIOSFODNN7EXAMPLE", "<AWS_ACCESS_KEY>"),
    }
    raw, tag = samples[name]
    assert tag in redact_text(raw)
