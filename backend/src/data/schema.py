"""Idempotent DDL for every table pinned in DESIGN §Data models, plus (T-20)
versioned, ledger-tracked migrations for schema changes made after the
initial table shapes below were pinned.

T-1 is the only ticket with ``backend/src/data/**`` in scope, so every table
later tickets need — ``runs`` (T-5/T-6), ``drafts``/``settings`` (T-8) — is
created now, up front; those tickets only ever read/write rows through
``data.db.get_connection``, never alter the schema.

Every statement in ``_TABLES``/``_ENUM_*`` is re-runnable: ``CREATE TABLE IF
NOT EXISTS``/``CREATE INDEX IF NOT EXISTS`` for tables and indexes, a ``DO``
block guarding each enum type against ``duplicate_object`` so re-running
``init_schema`` never errors.

SPEC R13 / DESIGN §Portal API's ``escalations_by_reason`` metric needs
``runs.reasons``. ``CREATE TABLE IF NOT EXISTS`` alone would NOT add this
column to a database whose ``runs`` table already existed before this
change (it's a no-op when the table is already there) — so ``_TABLES``
both declares the column for a from-scratch build AND migration ``0001``
(see ``migrations/0001_add_runs_reasons.sql``) runs a follow-up ``ALTER
TABLE ... ADD COLUMN IF NOT EXISTS`` so a database that predates this
column is upgraded in place the next time ``init_schema`` runs against it.
This is the general pattern any future column addition to an existing
table here should follow: the plain ``CREATE TABLE`` clause is only ever
consulted for a table that doesn't exist yet, so it can never by itself
carry a schema change to a database where the table already does — the
change belongs in a new numbered migration file instead.

T-20: schema changes made after the tables below were first pinned are no
longer inlined as one ad-hoc, unconditionally-re-executed SQL string.
Instead they live as individual, numbered ``.sql`` files under
``migrations/``, and a ``schema_migrations`` ledger table records which of
them have already run — so ``init_schema`` applies each migration file
EXACTLY ONCE, no matter how many times it's called (every container boot,
every test fixture), rather than re-executing every migration's SQL on
every call and relying on that SQL happening to be idempotent.

Everything here — ``_TABLES``, every enum ``DO`` block, the migration SQL
files, and the ``schema_migrations`` table itself — is deliberately
schema-UNqualified: it relies entirely on the connection's ``search_path``
(see ``data.db.get_connection``). T-16 gives each concurrent test process
its own private schema by pointing ``search_path`` at it; a table or ledger
name hardcoded to ``public`` (or any other fixed schema) would defeat that
isolation by making every process share one ledger. Production never sets
the test-only override, so ``search_path`` resolves to ``public`` there and
this is a no-op change in behavior for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

from data.embeddings import EMBEDDING_DIM

_ENUM_STAGE = """
DO $$ BEGIN
    CREATE TYPE stage_enum AS ENUM ('intake', 'extraction', 'sequencing', 'genealogy', 'complete');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

_ENUM_OUTCOME = """
DO $$ BEGIN
    CREATE TYPE outcome_enum AS ENUM (
        'auto_sent', 'gated_sent', 'rejected', 'escalated', 'off_topic'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

_ENUM_DRAFT = """
DO $$ BEGIN
    CREATE TYPE draft_enum AS ENUM ('pending', 'approved', 'rejected', 'auto_sent');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

_TABLES = f"""
CREATE TABLE IF NOT EXISTS cases (
    case_id text PRIMARY KEY,
    requester_email text NOT NULL,
    requester_name text,
    stage stage_enum NOT NULL,
    stage_entered_at date,
    last_updated date,
    eta_weeks int,
    dna_profile_available boolean,
    photos_available boolean
);
CREATE INDEX IF NOT EXISTS idx_cases_requester_email ON cases (requester_email);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id bigserial PRIMARY KEY,
    doc_slug text NOT NULL,
    chunk_index int NOT NULL,
    text text NOT NULL,
    embedding vector({EMBEDDING_DIM}) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_slug ON kb_chunks (doc_slug);

CREATE TABLE IF NOT EXISTS tickets_seen (
    ticket_id text NOT NULL,
    comment_id text NOT NULL,
    PRIMARY KEY (ticket_id, comment_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL,
    route text,
    confidence double precision,
    outcome outcome_enum,
    verifier_score double precision,
    trace_id text,
    received_at timestamptz,
    replied_at timestamptz,
    reasons text[] NOT NULL DEFAULT '{{}}'::text[]
);

CREATE TABLE IF NOT EXISTS drafts (
    id bigserial PRIMARY KEY,
    run_id bigint REFERENCES runs (id),
    body text,
    edited_body text,
    status draft_enum
);

CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value text
);
"""

# ---------------------------------------------------------------------------
# T-20: versioned migrations + ledger.
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Unqualified -- lands in whatever schema the connection's search_path
# currently points at (see module docstring: this is the T-16 coupling).
_LEDGER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""

# A fixed, arbitrary 64-bit key for `pg_advisory_xact_lock`. Advisory locks
# are cluster-wide, not schema-scoped -- that's intentional here: it only
# serializes the *timing* of "check the ledger, then apply+record" across
# concurrent processes that might resolve to the same schema (i.e. two
# production replicas booting against `public` at once); it never touches
# which schema's tables a process reads or writes, so it takes nothing away
# from T-16's per-process schema isolation. It auto-releases at the
# transaction boundary, which lines up with `init_schema`'s single
# `conn.commit()` -- no explicit unlock needed.
_MIGRATIONS_LOCK_KEY = 0x54_32305F4C4544  # "T20_LED" gibberish, just a fixed constant


@dataclass(frozen=True)
class Migration:
    """One numbered migration: a version, a human-readable name, and the
    SQL to run exactly once for a given version."""

    version: int
    name: str
    sql: str


def discover_migrations() -> list[Migration]:
    """Load every ``*.sql`` file under ``migrations/`` as a ``Migration``,
    ordered by ascending version.

    File names are ``<version>_<name>.sql`` (e.g.
    ``0001_add_runs_reasons.sql``); the zero-padded numeric prefix sorts
    lexicographically in the same order as numerically, so a plain sorted
    glob is sufficient. Resolved relative to this module's own file (not
    the current working directory), so discovery is identical whether
    called from a container's ``/app`` working directory or from pytest
    invoked at the repo root.
    """
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version_str, _, name = path.stem.partition("_")
        migrations.append(
            Migration(version=int(version_str), name=name, sql=path.read_text(encoding="utf-8"))
        )
    return migrations


def _apply_migrations(conn: psycopg.Connection, migrations: Sequence[Migration]) -> None:
    """Apply every migration in ``migrations`` that has no row yet in the
    ``schema_migrations`` ledger, in ascending version order, then record
    it -- so each migration's SQL runs EXACTLY ONCE per database/schema no
    matter how many times ``init_schema`` is called.

    Gated on the ledger row's existence, not on the migration SQL itself
    being idempotent -- a migration that is NOT safe to re-run is still
    only ever executed once, because a second call finds its version
    already recorded and skips it outright.

    The whole check-then-apply sequence runs inside one
    ``pg_advisory_xact_lock`` so two processes racing to boot against the
    same schema (a production concern -- see the module docstring; T-16
    already rules this out for tests) can't both observe "not yet applied"
    for the same version and both attempt to apply it. ``ON CONFLICT ...
    DO NOTHING`` on the insert is a cheap second safety net regardless.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATIONS_LOCK_KEY,))
        cur.execute(_LEDGER_TABLE_SQL)
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

        for migration in sorted(migrations, key=lambda m: m.version):
            if migration.version in applied:
                continue
            cur.execute(migration.sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
                "ON CONFLICT (version) DO NOTHING",
                (migration.version, migration.name),
            )


def init_schema(
    conn: psycopg.Connection,
    *,
    migrations: Sequence[Migration] | None = None,
) -> None:
    """Create every pinned table/type if it does not already exist, then
    apply every not-yet-applied migration exactly once (T-20).

    ``migrations`` defaults to ``discover_migrations()`` -- the real
    migration files under ``migrations/``. Callers outside this ticket's
    scope (``backend/tests/portal/conftest.py``,
    ``backend/tests/ingress/conftest.py``, ``deploy/backend/bootstrap.py``,
    ``data.seed.seed_all``) all call ``init_schema(conn)`` with no other
    arguments and get that default; the keyword-only ``migrations``
    override exists solely so tests in this ticket's own scope
    (``backend/tests/data/test_migrations.py``) can inject a fake
    migration without touching the real migration file list.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(_ENUM_STAGE)
        cur.execute(_ENUM_OUTCOME)
        cur.execute(_ENUM_DRAFT)
        cur.execute(_TABLES)
    _apply_migrations(conn, migrations if migrations is not None else discover_migrations())
    conn.commit()
