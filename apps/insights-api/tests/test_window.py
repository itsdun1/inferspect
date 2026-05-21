"""Unit tests for the window-string parser."""

from __future__ import annotations

from datetime import timedelta

import pytest
from insights_api.services.window import parse_window


def test_parses_minutes():
    assert parse_window("5m") == timedelta(minutes=5)


def test_parses_hours():
    assert parse_window("1h") == timedelta(hours=1)
    assert parse_window("24h") == timedelta(hours=24)


def test_parses_days():
    assert parse_window("7d") == timedelta(days=7)
    assert parse_window("30d") == timedelta(days=30)


def test_parses_seconds():
    assert parse_window("30s") == timedelta(seconds=30)


def test_rejects_empty():
    with pytest.raises(ValueError):
        parse_window("")


def test_rejects_unknown_unit():
    with pytest.raises(ValueError):
        parse_window("3y")


def test_rejects_non_positive():
    with pytest.raises(ValueError):
        parse_window("0h")


def test_rejects_garbage():
    with pytest.raises(ValueError):
        parse_window("garbage")
