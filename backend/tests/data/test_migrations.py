"""T-20 acceptance tests: versioned schema migrations + ledger.

Every test here runs against a throwaway, uuid-suffixed schema of its own
(never the current pytest process's own ``OTHRAM_TEST_SCHEMA`` schema),
using the exact override-then-restore-then-``DROP SCHEMA`` pattern already
established by ``backend/tests/data/test_schema_isolation.py`` — see
``_schema_scope`` below. Explicit teardown is required, not optional: the
crash-reaper in ``backend/tests/conftest.py`` only reaps names matching
``test_<slug>_<pid>`` (it checks ``pid_str.isdigit()`` on the trailing
segment), so a uuid-suffixed throwaway name would never be picked up if a
test failed to clean up after itself.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import pytest
from psycopg import sql

from data.db import TEST_SCHEMA_ENV_VAR, get_connection
from data.schema import Migration, _apply_migrations, discover_migrations, init_schema

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

# The exact pre-T-8 ``runs`` DDL (commit a0cd8e2, before 67776ba added
# ``reasons``) — no ``reasons`` column, and obviously no
# ``schema_migrations`` ledger (that table didn't exist until T-20).
# Hardcoded verbatim (rather than imported from `data.schema`) so this test
# proves the upgrade path against the actual historical shape, independent
# of any future refactor of the current schema module.
_PRE_T8_OUTCOME_ENUM_SQL = """
DO $$ BEGIN
    CREATE TYPE outcome_enum AS ENUM (
        'auto_sent', 'gated_sent', 'rejected', 'escalated', 'off_topic'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

_PRE_T8_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL,
    route text,
    confidence double precision,
    outcome outcome_enum,
    verifier_score double precision,
    trace_id text,
    received_at timestamptz,
    replied_at timestamptz
);
"""


@contextmanager
def _schema_scope(name: str) -> Iterator[str]:
    """Point ``OTHRAM_TEST_SCHEMA`` at a fresh throwaway schema for the
    duration of the block, restore the caller's own value afterward, and
    unconditionally ``DROP SCHEMA ... CASCADE`` the throwaway schema on
    the way out (success or failure)."""
    previous = os.environ.get(TEST_SCHEMA_ENV_VAR)
    os.environ[TEST_SCHEMA_ENV_VAR] = name
    try:
        yield name
    finally:
        if previous is not None:
            os.environ[TEST_SCHEMA_ENV_VAR] = previous
        else:
            os.environ.pop(TEST_SCHEMA_ENV_VAR, None)
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def _fresh_throwaway_name() -> str:
    return f"test_migrations_probe_{uuid.uuid4().hex[:8]}"


def _runs_columns(schema_name: str) -> list[tuple[str, str, str]]:
    """``(column_name, data_type, is_nullable)`` triples for the ``runs``
    table in ``schema_name``, ordered by column position. Filters by
    ``table_schema`` explicitly, so it is correct regardless of which
    schema the calling connection's ``search_path`` currently points at."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'runs' ORDER BY ordinal_position",
            (schema_name,),
        )
        return cur.fetchall()


def _ledger_rows(schema_name: str) -> list[tuple[int, str]]:
    """``(version, name)`` pairs recorded in ``schema_name``'s
    ``schema_migrations`` ledger, ordered by version. Schema-qualifies the
    table name explicitly so it works regardless of the calling
    connection's current ``search_path``."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT version, name FROM {}.schema_migrations ORDER BY version").format(
                sql.Identifier(schema_name)
            )
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Acceptance 1: numbered migration files + ledger, each applied exactly once.
# ---------------------------------------------------------------------------


def test_discover_migrations_finds_migration_1() -> None:
    migrations = discover_migrations()
    versions = [m.version for m in migrations]
    assert versions == sorted(versions), "migrations must be discovered in ascending order"
    assert 1 in versions
    first = next(m for m in migrations if m.version == 1)
    assert first.name == "add_runs_reasons"
    assert "ALTER TABLE runs" in first.sql
    assert "reasons" in first.sql


def test_fresh_database_records_migration_1_exactly_once() -> None:
    with _schema_scope(_fresh_throwaway_name()) as schema_name:
        with get_connection() as conn:
            init_schema(conn)
        assert _ledger_rows(schema_name) == [(1, "add_runs_reasons")]


def test_calling_init_schema_repeatedly_does_not_duplicate_ledger_rows() -> None:
    with _schema_scope(_fresh_throwaway_name()) as schema_name:
        with get_connection() as conn:
            init_schema(conn)
        with get_connection() as conn:
            init_schema(conn)
        with get_connection() as conn:
            init_schema(conn)
        assert _ledger_rows(schema_name) == [(1, "add_runs_reasons")]


# ---------------------------------------------------------------------------
# Acceptance 2 + 5: runs.reasons expressed as migration 1; a database created
# before it (built from the pre-T-8 schema) upgrades in place to the
# IDENTICAL schema a fresh database ends up with.
# ---------------------------------------------------------------------------


def test_fresh_database_and_upgraded_pre_t8_database_end_in_identical_schema() -> None:
    # Path A: a brand-new database, created purely via the current
    # init_schema (the from-scratch path: _TABLES already declares
    # `reasons` inline).
    with _schema_scope(_fresh_throwaway_name()) as fresh_schema:
        with get_connection() as conn:
            init_schema(conn)
        fresh_columns = _runs_columns(fresh_schema)
        fresh_ledger = _ledger_rows(fresh_schema)

    # Path B: a database built from the exact pre-T-8 shape (no `reasons`,
    # no `schema_migrations` at all), bypassing schema.py's current
    # _TABLES entirely, then upgraded by calling the CURRENT init_schema.
    with _schema_scope(_fresh_throwaway_name()) as upgraded_schema:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(_PRE_T8_OUTCOME_ENUM_SQL)
            cur.execute(_PRE_T8_RUNS_TABLE_SQL)
        with get_connection() as conn, conn.cursor() as cur:
            # Sanity precondition: genuinely pre-T-8 -- no reasons column,
            # no ledger table yet.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'runs' AND column_name = 'reasons'",
                (upgraded_schema,),
            )
            assert cur.fetchone() is None, "precondition failed: reasons already present"
            cur.execute(
                "SELECT to_regclass(%s)",
                (f"{upgraded_schema}.schema_migrations",),
            )
            row = cur.fetchone()
            assert row is not None and row[0] is None, "precondition failed: ledger already exists"

        with get_connection() as conn:
            init_schema(conn)  # the upgrade under test

        upgraded_columns = _runs_columns(upgraded_schema)
        upgraded_ledger = _ledger_rows(upgraded_schema)

    # Both paths converge on the identical end state.
    assert upgraded_columns == fresh_columns
    assert any(name == "reasons" for name, _dtype, _nullable in upgraded_columns)
    assert upgraded_ledger == fresh_ledger == [(1, "add_runs_reasons")]


# ---------------------------------------------------------------------------
# Acceptance 4: a deliberately NON-idempotent migration is applied exactly
# once across repeated init_schema calls.
# ---------------------------------------------------------------------------


def test_non_idempotent_migration_is_applied_exactly_once() -> None:
    # Deliberately has no IF NOT EXISTS / ON CONFLICT guard anywhere -- a
    # second raw execution of this SQL against the same schema would raise
    # (duplicate table). If the ledger, not the SQL's own idempotency, is
    # what prevents re-execution, calling init_schema twice must not raise
    # and must leave exactly one row behind.
    probe = Migration(
        version=999,
        name="non_idempotent_probe",
        sql="CREATE TABLE probe_marker (id int); INSERT INTO probe_marker VALUES (1);",
    )
    migrations = [*discover_migrations(), probe]

    with _schema_scope(_fresh_throwaway_name()) as schema_name:
        with get_connection() as conn:
            init_schema(conn, migrations=migrations)
        with get_connection() as conn:
            init_schema(conn, migrations=migrations)  # must NOT re-run probe.sql

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM probe_marker")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1

        assert _ledger_rows(schema_name).count((999, "non_idempotent_probe")) == 1


# ---------------------------------------------------------------------------
# Acceptance 3: bootstrap (repeated init_schema calls, as happens on every
# container boot) applies only migrations not yet in the ledger -- a
# migration recorded on an earlier "boot" is never re-applied, and a
# migration added later (simulating new code shipped) IS applied on the
# next "boot" without touching anything already recorded.
# ---------------------------------------------------------------------------


def test_repeated_boots_apply_only_newly_added_migrations() -> None:
    boot_a = Migration(
        version=900,
        name="boot_marker_a",
        sql="CREATE TABLE boot_marker_a (id int); INSERT INTO boot_marker_a VALUES (1);",
    )
    boot_b = Migration(
        version=901,
        name="boot_marker_b",
        sql="CREATE TABLE boot_marker_b (id int); INSERT INTO boot_marker_b VALUES (1);",
    )

    with _schema_scope(_fresh_throwaway_name()) as schema_name:
        # "Boot 1": only migration 900 exists in the (simulated) codebase.
        with get_connection() as conn:
            init_schema(conn, migrations=[*discover_migrations(), boot_a])

        # "Boot 2": container restarts, same migration set -- 900 (and the
        # real migration 1) must not be re-applied. If it were, the raw
        # non-idempotent SQL above would raise here.
        with get_connection() as conn:
            init_schema(conn, migrations=[*discover_migrations(), boot_a])

        # "Boot 3": a new migration (901) shipped in the codebase -- only
        # it should be newly applied; 900 must stay untouched.
        with get_connection() as conn:
            init_schema(conn, migrations=[*discover_migrations(), boot_a, boot_b])

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM boot_marker_a")
            row_a = cur.fetchone()
            cur.execute("SELECT count(*) FROM boot_marker_b")
            row_b = cur.fetchone()
        assert row_a is not None and row_a[0] == 1
        assert row_b is not None and row_b[0] == 1

        ledger_versions = {version for version, _name in _ledger_rows(schema_name)}
        assert ledger_versions == {1, 900, 901}


# ---------------------------------------------------------------------------
# Regression: a migration must never be recorded as "applied" in the ledger
# unless its own SQL genuinely ran first.
#
# A real-Postgres, real-`init_schema` failure-path test alone cannot prove
# this: today, any exception raised inside ``_apply_migrations`` propagates
# out of ``init_schema`` to ``data.db.get_connection``'s
# ``except Exception: conn.rollback(); raise``, which erases every
# statement issued since the connection last committed -- including a
# ledger INSERT -- *regardless of whether that INSERT ran before or after
# the migration's own SQL*. So "no ledger row survives a genuine SQL
# failure" would pass even if ``_apply_migrations`` recorded the ledger row
# before executing the migration's SQL; the accidental single-uncommitted-
# transaction rollback hides the ordering bug. (Confirmed by hand: swapping
# the INSERT and the migration's own ``cur.execute`` in ``_apply_migrations``
# left every other test in this module green.)
#
# These two tests instead observe ``_apply_migrations`` directly through a
# fake cursor that just logs/raises -- no real Postgres, no surrounding
# transaction to accidentally cover for a reordering -- so they fail on the
# reordering itself, and would also catch a future refactor that commits
# per-migration instead of once at the end of ``init_schema``.
# ---------------------------------------------------------------------------


class _SimulatedMigrationFailure(Exception):
    """Stands in for a real psycopg error (e.g. a syntax error) that a
    genuinely broken migration's SQL would raise."""


class _LoggingCursor:
    """Fake cursor: records every statement passed to ``execute`` in call
    order and, if constructed with a ``fail_on`` statement, raises instead
    of "running" that one statement -- exactly what a real cursor does when
    handed invalid SQL, except deterministic and requiring no database."""

    def __init__(self, log: list[str], fail_on: str | None) -> None:
        self._log = log
        self._fail_on = fail_on

    def execute(self, statement: str, params: object = None) -> None:
        self._log.append(statement)
        if statement == self._fail_on:
            raise _SimulatedMigrationFailure(statement)

    def fetchall(self) -> list[tuple[int]]:
        return []

    def __enter__(self) -> _LoggingCursor:
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False


class _LoggingConnection:
    """Fake ``psycopg.Connection``: hands out ``_LoggingCursor``s that all
    share one log list, so the call order across the whole
    ``_apply_migrations`` body is directly observable."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[str] = []
        self._fail_on = fail_on

    def cursor(self) -> _LoggingCursor:
        return _LoggingCursor(self.log, self._fail_on)


def test_apply_migrations_executes_migration_sql_before_recording_ledger_row() -> None:
    """If a future change ever records the ledger row before (or without)
    actually running the migration's SQL -- a plain reordering mistake, or
    a refactor that commits after each migration instead of once at the
    end of ``init_schema`` -- this test catches it directly."""
    probe_sql = "-- probe migration sql, never actually run against Postgres --"
    migration = Migration(version=1234, name="probe", sql=probe_sql)
    conn = _LoggingConnection()

    _apply_migrations(conn, [migration])  # type: ignore[arg-type]

    execute_index = conn.log.index(probe_sql)
    insert_index = next(
        i
        for i, statement in enumerate(conn.log)
        if statement.startswith("INSERT INTO schema_migrations")
    )
    assert execute_index < insert_index, (
        "the ledger row must be written strictly AFTER the migration's own "
        "SQL executes -- recording it first (or without executing the SQL "
        "at all) would mark a migration 'applied' that never actually ran"
    )


def test_apply_migrations_never_reaches_ledger_insert_when_migration_sql_raises() -> None:
    """If a migration's own SQL raises, ``_apply_migrations`` must never
    even attempt the ledger INSERT for it. Proven at the Python
    control-flow level -- an exception raised by ``cur.execute()`` skips
    every later statement in the same function body -- independent of
    whatever the surrounding connection/transaction later does with that
    exception."""
    failing_sql = "THIS IS NOT VALID SQL;"
    migration = Migration(version=5678, name="doomed", sql=failing_sql)
    conn = _LoggingConnection(fail_on=failing_sql)

    with pytest.raises(_SimulatedMigrationFailure):
        _apply_migrations(conn, [migration])  # type: ignore[arg-type]

    assert not any(
        statement.startswith("INSERT INTO schema_migrations") for statement in conn.log
    )
