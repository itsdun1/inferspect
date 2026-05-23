"""Async SQLAlchemy session factory + the FastAPI dependency.

Mirrors ``chat_service.db.session`` — the session yielded by ``get_session``
owns the transaction boundary: commit on clean return, rollback on exception.
Repositories never call ``commit``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _SessionLocal
    _engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    _SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("engine not initialized; call init_engine() in lifespan first")
    return _engine


def get_session_local() -> async_sessionmaker[AsyncSession]:
    if _SessionLocal is None:
        raise RuntimeError("SessionLocal not initialized")
    return _SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    if _SessionLocal is None:
        raise RuntimeError("SessionLocal not initialized")
    async with _SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
