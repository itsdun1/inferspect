"""SQLAlchemy ORM models for the insights-api / operator console.

Two Base classes here, intentionally:

  - ``Base``              — tables this service OWNS. ``Operator`` only;
                            we ``create_all`` this in lifespan.
  - ``SharedBase``        — tables OWNED by chat-service (``users``,
                            ``conversations``) that we read for cross-tenant
                            queries. We map them so SQLAlchemy can return
                            typed rows, but we never ``create_all`` them
                            here — chat-service owns the schema.

The operator console (`operators` table) is intentionally separate from
chat-service's `users` table — operators are Ollive employees, not
end-users of any customer's chat product. They share zero principals.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

# NOTE: importing ``fastapi_users.db`` first warms its module cache so the
# downstream import of ``fastapi_users_db_sqlalchemy`` (which itself imports
# ``fastapi_users.db.base``) doesn't race with the conditional re-export in
# ``fastapi_users.db.__init__``. Without this, certain pytest collection
# orders end up with ``fastapi_users.db.SQLAlchemyUserDatabase`` unbound.
import fastapi_users.db  # noqa: F401

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Tables this service owns. Only ``operators`` lives here."""


class SharedBase(DeclarativeBase):
    """Tables chat-service owns; mapped here for read access only."""


class Operator(SQLAlchemyBaseUserTableUUID, Base):
    """A platform-vendor employee allowed into the operator console."""

    __tablename__ = "operators"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(SharedBase):
    """Mirror of chat-service's ``users`` table (read-only from this service)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("role IN ('user','admin')", name="users_role_check"),)


class Conversation(SharedBase):
    """Mirror of chat-service's ``conversations`` table (read-only from this service)."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','cancelled','completed')", name="conversations_status_check"
        ),
        Index("ix_conversations_user_updated", "user_id", "updated_at"),
    )
