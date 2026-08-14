"""Database connection helper shared by the whole data layer.

T-1 is the only ticket with ``backend/src/data/**`` in scope, so later
tickets that read/write ``runs`` (T-5/T-6) or ``drafts``/``settings`` (T-8)
import ``get_connection`` from here rather than opening their own psycopg
connection — the DSN resolution and the pgvector adapter registration live
in exactly one place.

T-16 adds per-process Postgres schema isolation for concurrent test runs.
``OTHRAM_TEST_SCHEMA`` is a test-time-only signal: it is set exactly once,
by ``backend/tests/conftest.py`` at module-import time (see that file for
the derivation), and read nowhere else in ``backend/src/**`` but here. In
production nothing ever sets it, ``os.environ.get`` returns ``None``, the
``if test_schema:`` branch below never runs, and every connection behaves
exactly as it did before this change — same DSN, same default
(``public``) schema, no new query on the connect path. This file has no
import-time dependency on ``backend/tests/**`` — it only reads an
environment variable a test process happens to have set.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql

DEFAULT_DSN = "postgresql://othram:othram@localhost:5432/othram"

# Test-time-only signal (see module docstring). Never set in production;
# never read anywhere outside this module.
TEST_SCHEMA_ENV_VAR = "OTHRAM_TEST_SCHEMA"


def get_dsn() -> str:
    """Resolve the Postgres DSN, honoring the ``DATABASE_URL`` override."""
    return os.getenv("DATABASE_URL", DEFAULT_DSN)


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """Yield a connection with the pgvector adapter registered.

    Commits on clean exit, rolls back on exception, always closes. Ensures
    the ``vector`` extension exists before registering the adapter so this
    never races schema setup regardless of call order (safe to call before
    ``init_schema`` has ever run).

    If ``OTHRAM_TEST_SCHEMA`` is set (test-time only — see module
    docstring), the connection's ``search_path`` is switched to that
    schema (created if needed) so every unqualified table/type reference
    made through this connection — including ``init_schema``'s DDL and
    ``data.seed.seed_all``'s TRUNCATE/INSERT — lands in a private,
    per-process copy instead of the shared ``public`` schema. Absent that
    variable (the production case), this block is skipped entirely and
    behavior is unchanged from before T-16.
    """
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            # Pinned to ``public`` explicitly, and run BEFORE any
            # search_path switch below: without the explicit SCHEMA
            # clause, CREATE EXTENSION installs into whatever schema is
            # first on search_path at the moment it runs. If that were a
            # per-test schema, the `vector` type would live inside one
            # test process's private, throwaway schema — and every other
            # process would find it missing the moment that schema gets
            # dropped at teardown. Pinning to ``public`` unconditionally
            # makes the extension's location independent of search_path
            # and of call order.
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
            test_schema = os.environ.get(TEST_SCHEMA_ENV_VAR)
            if test_schema:
                cur.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(test_schema)
                    )
                )
                cur.execute(
                    sql.SQL("SET search_path TO {}, public").format(
                        sql.Identifier(test_schema)
                    )
                )
        conn.commit()
        register_vector(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
