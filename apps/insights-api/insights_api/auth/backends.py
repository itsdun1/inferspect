"""Operator auth backends — cookie-based JWT.

Different cookie name (`console_session`) and different secret from
chat-service's `chat_session` cookie so the two services issue independent
sessions even when they share a domain. An HttpOnly cookie protects the
token from JS access; `samesite=lax` keeps CSRF protection for navigation.
"""

from __future__ import annotations

from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy

from insights_api.config import settings


cookie_transport = CookieTransport(
    cookie_name="console_session",
    cookie_max_age=settings.console_jwt_lifetime_seconds,
    cookie_secure=settings.cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def _strategy() -> JWTStrategy:
    return JWTStrategy(
        secret=settings.console_jwt_secret,
        lifetime_seconds=settings.console_jwt_lifetime_seconds,
    )


auth_backend = AuthenticationBackend(
    name="cookie-jwt",
    transport=cookie_transport,
    get_strategy=_strategy,
)
