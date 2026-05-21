"""Bootstrap admin from environment — idempotent.

Reads ``BOOTSTRAP_ADMIN_EMAIL`` and ``BOOTSTRAP_ADMIN_PASSWORD``. If the user
already exists, we don't change their password but we DO promote them to
admin (so re-running with the same email is safe and recovers from a stuck
state). If unset, we silently skip — the demo works fine if you register
users via the UI.
"""

from __future__ import annotations

import logging

from fastapi_users.password import PasswordHelper
from sqlalchemy.ext.asyncio import async_sessionmaker

from chat_service.config import settings
from chat_service.db.models import User
from chat_service.repositories import user_repository

log = logging.getLogger(__name__)


async def bootstrap_admin(SessionLocal: async_sessionmaker) -> None:
    email = settings.bootstrap_admin_email
    password = settings.bootstrap_admin_password
    if not email or not password:
        log.info("bootstrap_admin: no admin email/password configured, skipping")
        return

    async with SessionLocal() as session:
        existing = await user_repository.get_by_email(session, email)
        if existing is None:
            helper = PasswordHelper()
            hashed = helper.hash(password)
            user = User(
                email=email,
                hashed_password=hashed,
                role="admin",
                is_active=True,
                is_superuser=True,
                is_verified=True,
            )
            session.add(user)
            await session.commit()
            log.info("bootstrap_admin: created admin %s", email)
            return

        # Promote if not already admin.
        promoted = False
        if existing.role != "admin":
            existing.role = "admin"
            promoted = True
        if not existing.is_superuser:
            existing.is_superuser = True
            promoted = True
        if promoted:
            await session.commit()
            log.info("bootstrap_admin: promoted existing user %s to admin", email)
        else:
            log.info("bootstrap_admin: %s already admin, no changes", email)
