"""Centralized helper for optional-dependency integration modules.

Each integration submodule (``chatbot_sdk.integrations.<vendor>``) calls
``_require_extra`` at import time. If the vendor library isn't installed the
user gets a focused ``pip install inferspect-sdk[<extra>]`` hint instead of a
bare ``ModuleNotFoundError`` further down the file."""

from __future__ import annotations

DISTRIBUTION_NAME = "inferspect-sdk"


def _require_extra(extra_name: str, *module_names: str) -> None:
    """Raise ImportError with a ``pip install`` hint if any module is missing."""
    for module in module_names:
        try:
            __import__(module)
        except ImportError:
            raise ImportError(
                f"`{module}` is required for the {DISTRIBUTION_NAME} {extra_name} integration. "
                f"Install it with: pip install {DISTRIBUTION_NAME}[{extra_name}]"
            ) from None
