"""Resolve SDK API keys to a client name via env-injected map."""
from __future__ import annotations
import json
import logging

log = logging.getLogger(__name__)


class ApiKeyResolver:
    """Parse SDK_API_KEYS_JSON (a JSON object {key: client_name}) and serve lookups."""
    def __init__(self, raw_json: str) -> None:
        self._map: dict[str, str] = {}
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    self._map = {str(k): str(v) for k, v in parsed.items()}
                else:
                    log.warning("SDK_API_KEYS_JSON must be a JSON object; got %s", type(parsed).__name__)
            except json.JSONDecodeError as e:
                log.error("SDK_API_KEYS_JSON failed to parse: %s", e)
        log.info("api key resolver loaded with %d key(s)", len(self._map))

    def resolve(self, key: str | None) -> str | None:
        """Return the client_name for this key, or None if unknown."""
        if not key:
            return None
        return self._map.get(key)

    @property
    def has_any_keys(self) -> bool:
        return bool(self._map)
