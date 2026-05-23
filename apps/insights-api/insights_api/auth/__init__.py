"""Operator auth package — fastapi-users integration with cookie-based JWT.

External surface:
  - ``fastapi_users``  — the FastAPIUsers instance, exposes routers
  - ``current_active_operator``, ``current_superuser_operator``  — dependencies
  - ``bootstrap_operator``  — idempotent operator-from-env startup hook
  - ``operator_router``  — the mounted /auth router

This is the *platform vendor's* auth surface — separate from any customer's
end-user auth. Different cookie name, different JWT secret, different table.
"""

from insights_api.auth.bootstrap import bootstrap_operator
from insights_api.auth.deps import (
    current_active_operator,
    current_superuser_operator,
    fastapi_users,
)
from insights_api.auth.router import operator_router

__all__ = [
    "bootstrap_operator",
    "current_active_operator",
    "current_superuser_operator",
    "fastapi_users",
    "operator_router",
]
