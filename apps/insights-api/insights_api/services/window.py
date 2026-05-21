"""Window-string parser used by every metric endpoint.

Accepts terse strings like ``"5m"``, ``"1h"``, ``"24h"``, ``"7d"``, ``"30d"``
and returns a ``timedelta``. Centralized here so the controllers stay thin.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_PATTERN = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_UNIT_TO_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(window: str) -> timedelta:
    """Parse a window string. Raises ``ValueError`` on invalid input."""
    if not window:
        raise ValueError("window required")
    m = _PATTERN.match(window.strip())
    if not m:
        raise ValueError(
            f"invalid window {window!r}; expected e.g. 5m, 1h, 24h, 7d, 30d"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if n <= 0:
        raise ValueError("window must be positive")
    return timedelta(seconds=n * _UNIT_TO_SECONDS[unit])


def since_for(window: str, *, now: datetime | None = None) -> datetime:
    """Convenience: parse a window and return ``now - delta`` (UTC)."""
    delta = parse_window(window)
    base = now or datetime.now(UTC)
    return base - delta
