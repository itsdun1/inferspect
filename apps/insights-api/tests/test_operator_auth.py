"""Operator auth — bootstrap, login, /auth/users/me, logout, admin gate.

We swap insights-api's Postgres engine for an aiosqlite in-memory DB and
patch the ClickHouse + SDK init so the lifespan doesn't reach any external
service. The full auth stack (fastapi-users + cookie JWT) then runs for
real over an ASGI test client.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import insights_api.db.session as db_session
import insights_api.main as main_module
from insights_api.config import settings
from insights_api.db.models import SharedBase


# Shared in-memory aiosqlite DB; the URI cache=shared keeps it alive across
# the engine's connection pool for the duration of a test.
_SQLITE_URL = "sqlite+aiosqlite:///file:operator_auth_test?mode=memory&cache=shared&uri=true"


@pytest.fixture
async def app(monkeypatch):
    # Force sqlite URL via init_engine wrapper.
    original_init = db_session.init_engine

    def init_for_test(_url, **kwargs):
        return original_init(_SQLITE_URL, **kwargs)

    monkeypatch.setattr(db_session, "init_engine", init_for_test)
    monkeypatch.setattr(main_module, "init_engine", init_for_test)

    # No ClickHouse — replace the factory with a no-op client.
    async def no_ch_client():
        class _Dummy:
            async def close(self):
                pass
        return _Dummy()

    monkeypatch.setattr(main_module, "_ch_client_factory", no_ch_client)

    # Bootstrap creds.
    monkeypatch.setattr(settings, "console_bootstrap_email", "op@ollive.example.com")
    monkeypatch.setattr(settings, "console_bootstrap_password", "operator-pw-1")
    monkeypatch.setattr(settings, "sdk_api_key", None)
    monkeypatch.setattr(settings, "ingestion_url", "http://127.0.0.1:1/disabled")

    application = main_module.create_app()

    # Drive FastAPI's lifespan context manually so init_engine + bootstrap run.
    # FastAPI exposes the lifespan callable via ``router.lifespan_context``.
    async with application.router.lifespan_context(application):
        # The chat-service-owned tables (users/conversations) don't exist on
        # our sqlite engine; create them now since the admin endpoints read
        # from them.
        engine = db_session.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(SharedBase.metadata.create_all)

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield application, client


async def test_bootstrap_login_me_logout_and_admin_gate(app):
    application, client = app

    # ── unauthenticated /admin/conversations must be 401 ──
    r = await client.get("/admin/conversations")
    assert r.status_code == 401, r.text

    # ── login as bootstrapped operator ──
    r = await client.post(
        "/auth/login",
        data={"username": "op@ollive.example.com", "password": "operator-pw-1"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code in (200, 204), r.text
    has_cookie = (
        "console_session" in client.cookies
        or any("console_session" in v for v in r.headers.get_list("set-cookie"))
    )
    assert has_cookie, dict(r.headers)

    # ── /auth/users/me returns the bootstrapped operator ──
    r = await client.get("/auth/users/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "op@ollive.example.com"
    assert body["is_superuser"] is True
    assert body["is_active"] is True

    # ── /admin/conversations passes the auth gate (empty list ok) ──
    r = await client.get("/admin/conversations")
    assert r.status_code == 200, r.text
    assert r.json() == []

    # ── /admin/users returns the empty list ──
    r = await client.get("/admin/users")
    assert r.status_code == 200, r.text
    assert r.json() == []

    # ── logout clears the cookie ──
    r = await client.post("/auth/logout")
    assert r.status_code in (200, 204), r.text

    client.cookies.clear()
    r = await client.get("/admin/conversations")
    assert r.status_code == 401


async def test_admin_synthetic_requires_sdk_logger(app):
    application, client = app

    # Login first.
    r = await client.post(
        "/auth/login",
        data={"username": "op@ollive.example.com", "password": "operator-pw-1"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code in (200, 204)

    # Force the SDK logger to None — the endpoint returns 503 instead of crashing.
    application.state.sdk_logger = None
    r = await client.post("/admin/synthetic", json={"count": 1})
    assert r.status_code == 503, r.text
