"""Database connection helper shared by the whole data layer.

T-1 is the only ticket with ``backend/src/data/**`` in scope, so later
tickets that read/write ``runs`` (T-5/T-6) or ``drafts``/``settings`` (T-8)
import ``get_connection`` from here rather than opening their own psycopg
connection — the DSN resolution and the pgvector adapter registration live
in exactly one place.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector

DEFAULT_DSN = "postgresql://othram:othram@localhost:5432/othram"


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
    """
    conn = psycopg.connect(get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        register_vector(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
