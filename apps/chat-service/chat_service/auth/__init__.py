"""Auth package — fastapi-users integration with cookie-based JWT.

External surface:
  - ``fastapi_users``  — the FastAPIUsers instance, exposes routers
  - ``current_active_user``, ``current_admin_user``  — dependencies
  - ``bootstrap_admin``  — idempotent admin-from-env startup hook
"""

from chat_service.auth.deps import current_active_user, current_admin_user, fastapi_users
from chat_service.auth.router import auth_router
from chat_service.auth.bootstrap import bootstrap_admin

__all__ = [
    "auth_router",
    "bootstrap_admin",
    "current_active_user",
    "current_admin_user",
    "fastapi_users",
]
