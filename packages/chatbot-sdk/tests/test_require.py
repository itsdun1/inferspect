"""Tests for the ``_require_extra`` helper."""

from __future__ import annotations

import pytest

from chatbot_sdk._require import _require_extra


def test_require_extra_raises_for_missing_module():
    with pytest.raises(ImportError) as exc:
        _require_extra("xyz", "definitely_not_installed_module_xyz")
    msg = str(exc.value)
    assert "pip install inferspect-sdk[xyz]" in msg
    assert "definitely_not_installed_module_xyz" in msg


def test_require_extra_passes_for_installed_module():
    # ``json`` ships with the stdlib so this always succeeds.
    _require_extra("stdlib", "json")


def test_require_extra_multiple_modules_first_missing():
    with pytest.raises(ImportError) as exc:
        _require_extra("multi", "json", "another_missing_module")
    assert "another_missing_module" in str(exc.value)
    assert "pip install inferspect-sdk[multi]" in str(exc.value)
