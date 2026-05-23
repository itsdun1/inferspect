"""Bootstrap operator from environment — idempotent.

Reads ``CONSOLE_BOOTSTRAP_EMAIL`` and ``CONSOLE_BOOTSTRAP_PASSWORD``. If the
operator already exists, we don't change their password but we DO promote
them to superuser (so re-running with the same email is safe and recovers
from a stuck state). If unset, we silently skip — useful in tests.

This is the only mechanism for creating operators in Phase D — there is no
public sign-up to the platform console.
"""

from __future__ import annotations

import logging

from fastapi_users.password import PasswordHelper
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from insights_api.config import settings
from insights_api.db.models import Operator

log = logging.getLogger(__name__)


async def bootstrap_operator(SessionLocal: async_sessionmaker) -> None:
    email = settings.console_bootstrap_email
    password = settings.console_bootstrap_password
    if not email or not password:
        log.info("bootstrap_operator: no email/password configured, skipping")
        return

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Operator).where(Operator.email == email))
        ).scalar_one_or_none()
        if existing is None:
            helper = PasswordHelper()
            hashed = helper.hash(password)
            op = Operator(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_superuser=True,
                is_verified=True,
            )
            session.add(op)
            await session.commit()
            log.info("bootstrap_operator: created operator %s", email)
            return

        if not existing.is_superuser:
            existing.is_superuser = True
            await session.commit()
            log.info("bootstrap_operator: promoted existing operator %s to superuser", email)
        else:
            log.info("bootstrap_operator: %s already superuser, no changes", email)
