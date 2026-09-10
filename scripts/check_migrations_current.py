"""Fail if the models have drifted from the migrations.

Runs Alembic's autogenerate comparison against the migrated database and asserts the diff
is empty. A model change with no accompanying migration is a silent break: local tests
pass against a schema created from metadata, while the deployed database is missing the
column.

Exit codes: 0 in sync, 1 drift detected, 2 could not reach the database.
"""

from __future__ import annotations

import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import SQLAlchemyError

from gri.db import get_engine
from gri.models import Base


def main() -> int:
    try:
        engine = get_engine()
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diff = compare_metadata(context, Base.metadata)
    except SQLAlchemyError as exc:
        print(f"error: could not reach the database: {exc}", file=sys.stderr)
        return 2

    # alembic_version is managed by Alembic itself and is not part of Base.metadata.
    diff = [d for d in diff if "alembic_version" not in str(d)]

    if diff:
        print("models have drifted from migrations:", file=sys.stderr)
        for entry in diff:
            print(f"  {entry}", file=sys.stderr)
        print(
            "\nrun: alembic revision --autogenerate -m 'describe the change'",
            file=sys.stderr,
        )
        return 1

    print("migrations are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
