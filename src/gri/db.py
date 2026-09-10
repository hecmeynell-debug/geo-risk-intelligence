"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from gri.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=settings.debug,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def check_database(session: Session) -> dict[str, object]:
    """Report connectivity, the pgvector extension, and applied migration revision.

    Used by ``/health``. Reporting the Alembic revision matters more than it looks: a
    silently un-migrated database is the failure mode most likely to make the stack look
    fine while behaving wrongly.
    """
    session.execute(text("SELECT 1"))

    vector_version = session.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one_or_none()

    revision = session.execute(
        text(
            "SELECT version_num FROM alembic_version"
            " WHERE EXISTS (SELECT 1 FROM information_schema.tables"
            "               WHERE table_name = 'alembic_version')"
        )
    ).scalar_one_or_none()

    return {
        "connected": True,
        "pgvector_installed": vector_version is not None,
        "pgvector_version": vector_version,
        "alembic_revision": revision,
    }


def reset_engine() -> None:
    """Drop cached engine and session factory. Test-support only."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
