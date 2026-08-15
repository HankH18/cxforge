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

T-24: ``OTHRAM_TEST_SCHEMA`` being *set* used to be sufficient, on its own,
to redirect ``search_path``. That is convention, not structure — a
process that merely inherits a leaked ``OTHRAM_TEST_SCHEMA`` value (e.g.
copied into a subprocess's environment, or left over from a shell) would
silently have every query redirected to that schema, in production, with
no test ever having asked for it. ``_running_under_pytest`` below is a
second, independent condition that must ALSO hold before the override is
honored — see that function's docstring for exactly what signal it checks
and why the obvious-looking alternatives (``PYTEST_CURRENT_TEST``, a bare
``"pytest" in sys.modules`` check) were rejected.
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


def _running_under_pytest() -> bool:
    """True only while THIS process is inside a live ``pytest`` run —
    from before the first conftest.py is imported through the end of
    ``pytest_sessionfinish`` — false otherwise. This is T-24's structural
    gate: ``OTHRAM_TEST_SCHEMA`` is honored only when this is also true.

    Checks ``PYTEST_VERSION`` in ``os.environ``, not ``PYTEST_CURRENT_TEST``
    and not ``"pytest" in sys.modules``. Both alternatives were evaluated
    and rejected for concrete, empirically-confirmed reasons:

    * ``PYTEST_CURRENT_TEST`` (the signal the ticket itself suggested) is
      set by pytest per TEST ITEM only — during collection and inside each
      test's setup/call/teardown, but NOT during ``pytest_sessionstart`` or
      ``pytest_sessionfinish``. ``backend/tests/conftest.py`` creates and
      drops this process's private schema in exactly those two session
      hooks (see its module docstring). Gating on ``PYTEST_CURRENT_TEST``
      would make the override fall back to the default schema during
      session-start/finish, silently corrupting the shared ``public``
      schema or leaking orphaned per-process schemas — precisely what T-16
      and T-23 exist to prevent. Confirmed empirically with a throwaway
      probe plugin: ``PYTEST_CURRENT_TEST`` reads ``None`` inside both
      ``pytest_sessionstart`` and ``pytest_sessionfinish``, and only gets a
      value once an actual test item starts running.
    * ``"pytest" in sys.modules`` is true for the right span of time, but
      it only proves pytest was *imported* somewhere in this interpreter,
      not that it is actively *running* a session — e.g. it stays true
      forever after an ``import pytest`` for unrelated reasons (a fixture
      library, a stray top-level import) even once no session is live, and
      it has no natural "off" transition the way an env var popped in a
      ``finally`` block does.
    * ``PYTEST_VERSION`` is an environment variable pytest itself sets, in
      ``_pytest.config._main``, before ``_prepareconfig`` (i.e. before any
      conftest.py is imported) and pops in a ``finally`` block that only
      runs after ``pytest_cmdline_main`` — which runs ``pytest_sessionstart``
      and ``pytest_sessionfinish`` — returns. So it is set for the WHOLE
      process lifetime this gate needs to cover, confirmed empirically with
      the same throwaway probe: present (pytest's own version string) at
      ``pytest_sessionstart``, at every test item, and still present at
      ``pytest_sessionfinish``; absent both before ``pytest.main()`` is
      called and after it returns. It also correctly nests (pytest saves
      and restores any pre-existing value), which matters for the child
      pytest subprocesses ``backend/tests/data/test_concurrency.py`` and
      ``backend/tests/data/test_schema_isolation_inheritance.py`` spawn.

    What this proves: the current process is (or, for the duration of this
    call, behaves exactly as) a live pytest run. What it does NOT prove:
    that any particular test wants schema isolation, or that
    ``OTHRAM_TEST_SCHEMA`` itself was set intentionally rather than
    inherited — that half of the gate is still ``TEST_SCHEMA_ENV_VAR``
    being present, checked separately by the caller. Both conditions are
    required; neither alone is treated as sufficient.
    """
    return "PYTEST_VERSION" in os.environ


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

    If ``OTHRAM_TEST_SCHEMA`` is set AND ``_running_under_pytest()`` is
    true (T-24's structural gate — see that function's docstring and the
    module docstring), the connection's ``search_path`` is switched to
    that schema (created if needed) so every unqualified table/type
    reference made through this connection — including ``init_schema``'s
    DDL and ``data.seed.seed_all``'s TRUNCATE/INSERT — lands in a private,
    per-process copy instead of the shared ``public`` schema. Absent
    either condition (the production case, and the "leaked env var in a
    non-pytest process" case T-24 closes), this block is skipped entirely
    and behavior is unchanged from before T-16.
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
            if test_schema and _running_under_pytest():
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
