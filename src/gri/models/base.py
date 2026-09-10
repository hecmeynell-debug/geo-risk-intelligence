"""Declarative base, naming conventions, and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from gri.taxonomy import sorted_values

# Deterministic constraint names keep Alembic autogenerate diffs stable and make
# constraint violations readable in logs.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    """A UUID primary key generated application-side."""
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def one_of(column: str, values: frozenset[str] | tuple[str, ...], name: str) -> CheckConstraint:
    """Build an ``IN (...)`` CHECK constraint from a closed vocabulary.

    Generating these from :mod:`gri.taxonomy` rather than hand-writing the SQL means the
    database and the application cannot drift apart -- in particular it means the NG-4
    guarantee (no ``person`` entity type) is enforced by the database, not just by
    convention. NULL passes; use ``nullable=False`` on the column where the value is
    required.
    """
    rendered = ", ".join(f"'{value}'" for value in sorted_values(values))
    return CheckConstraint(f"{column} IS NULL OR {column} IN ({rendered})", name=name)


def sha256_hex(column: str, name: str) -> CheckConstraint:
    """Constrain a column to a lowercase 64-character hex digest."""
    return CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'", name=name)
