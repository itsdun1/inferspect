"""Unit tests for the ApiKeyResolver.

The resolver parses an env-injected JSON object mapping SDK keys to client
names and serves lookups. These tests cover the four corners:
  - happy path: known key resolves to its client_name
  - unknown key: returns None
  - empty map: returns None (caller decides what to do — deps.py treats it
    as 'no auth configured, allow through')
  - malformed JSON: resolver initializes with an empty map (never crashes)
"""

from __future__ import annotations

from ingestion_service.services.auth_service import ApiKeyResolver


def test_known_key_resolves_to_client_name():
    resolver = ApiKeyResolver('{"osk_alice_8f3a": "alice-corp", "osk_chatsvc": "chat-service"}')
    assert resolver.has_any_keys
    assert resolver.resolve("osk_alice_8f3a") == "alice-corp"
    assert resolver.resolve("osk_chatsvc") == "chat-service"


def test_unknown_key_returns_none():
    resolver = ApiKeyResolver('{"osk_alice_8f3a": "alice-corp"}')
    assert resolver.resolve("not-a-real-key") is None


def test_none_or_empty_key_returns_none():
    resolver = ApiKeyResolver('{"osk_alice_8f3a": "alice-corp"}')
    assert resolver.resolve(None) is None
    assert resolver.resolve("") is None


def test_empty_map_resolves_to_none():
    resolver = ApiKeyResolver("{}")
    assert not resolver.has_any_keys
    assert resolver.resolve("any-key") is None


def test_empty_string_initializes_empty_map():
    resolver = ApiKeyResolver("")
    assert not resolver.has_any_keys
    assert resolver.resolve("any-key") is None


def test_malformed_json_initializes_empty_map():
    # Bad JSON shouldn't crash the service at startup — log + fall back to empty.
    resolver = ApiKeyResolver("{not-valid-json}")
    assert not resolver.has_any_keys
    assert resolver.resolve("any-key") is None


def test_non_object_json_initializes_empty_map():
    # JSON arrays / scalars are not a {key: client_name} map.
    resolver = ApiKeyResolver('["osk_alice_8f3a"]')
    assert not resolver.has_any_keys
    assert resolver.resolve("osk_alice_8f3a") is None


def test_non_string_values_are_coerced_to_strings():
    # Defensive: if someone writes `{"key": 42}`, we don't blow up; we coerce.
    resolver = ApiKeyResolver('{"key": 42}')
    assert resolver.resolve("key") == "42"
