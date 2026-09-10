from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gri.models import Base

DEFAULT_TEST_URL = "postgresql+psycopg://gri:gri@localhost:5432/gri"


def _test_database_url() -> str:
    return os.environ.get(
        "GRI_TEST_DATABASE_URL", os.environ.get("GRI_DATABASE_URL", DEFAULT_TEST_URL)
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A live Postgres engine, or skip the test.

    Integration tests assert that the CHECK constraints actually reject bad rows. That
    cannot be verified against metadata alone -- it needs a real database, because the
    guarantee we care about is enforced by Postgres, not by Python.
    """
    url = _test_database_url()
    eng = create_engine(url, poolclass=None, future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"no Postgres at {url}: {exc}")
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def schema(engine: Engine) -> Iterator[Engine]:
    """Build the schema in a dedicated test schema, and tear it down afterwards."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("DROP SCHEMA IF EXISTS gri_test CASCADE"))
        conn.execute(text("CREATE SCHEMA gri_test"))

    test_engine = engine.execution_options(schema_translate_map={None: "gri_test"})
    Base.metadata.create_all(test_engine)
    yield engine

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS gri_test CASCADE"))


@pytest.fixture
def session(schema: Engine) -> Iterator[Session]:
    scoped = schema.execution_options(schema_translate_map={None: "gri_test"})
    factory = sessionmaker(bind=scoped, expire_on_commit=False)
    sess = factory()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
