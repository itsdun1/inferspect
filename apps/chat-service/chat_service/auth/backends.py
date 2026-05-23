"""Auth backends — cookie-based JWT.

We deliberately put the JWT in an HttpOnly cookie (not localStorage) so the
frontend's JS can't read it — protects against XSS token theft. The cookie is
also ``samesite=lax`` so CSRF protection holds for navigation but POSTs from
our own origin work."""

from __future__ import annotations

from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy

from chat_service.config import settings


cookie_transport = CookieTransport(
    cookie_name="chat_session",
    cookie_max_age=settings.jwt_lifetime_seconds,
    cookie_secure=settings.cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def _strategy() -> JWTStrategy:
    return JWTStrategy(
        secret=settings.jwt_secret,
        lifetime_seconds=settings.jwt_lifetime_seconds,
    )


auth_backend = AuthenticationBackend(
    name="cookie-jwt",
    transport=cookie_transport,
    get_strategy=_strategy,
)
