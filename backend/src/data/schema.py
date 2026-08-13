"""Idempotent DDL for every table pinned in DESIGN §Data models.

T-1 is the only ticket with ``backend/src/data/**`` in scope, so every table
later tickets need — ``runs`` (T-5/T-6), ``drafts``/``settings`` (T-8) — is
created now, up front; those tickets only ever read/write rows through
``data.db.get_connection``, never alter the schema.

Every statement is re-runnable: ``CREATE TABLE IF NOT EXISTS``/``CREATE INDEX
IF NOT EXISTS`` for tables and indexes, a ``DO`` block guarding each enum
type against ``duplicate_object`` so re-running ``init_schema`` never errors.

SPEC R13 / DESIGN §Portal API's ``escalations_by_reason`` metric needs
``runs.reasons`` (added below, closing that gap). ``CREATE TABLE IF NOT
EXISTS`` alone would NOT add this column to a database whose ``runs`` table
already exists from before this change (it's a no-op when the table is
already there) — so ``_TABLES`` both declares the column for a from-scratch
build AND ``init_schema`` runs a follow-up ``ALTER TABLE ... ADD COLUMN IF
NOT EXISTS`` (``_MIGRATIONS`` below) so an existing, already-populated
database is upgraded in place the next time ``init_schema`` runs against it.
This is the general pattern any future column addition to an existing table
here should follow: the plain ``CREATE TABLE`` clause is only ever consulted
for a table that doesn't exist yet, so it can never by itself carry a schema
change to a database where the table already does.
"""

from __future__ import annotations

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

# Upgrades a database whose ``runs`` table was created before this column
# existed (see the module docstring: the ``CREATE TABLE IF NOT EXISTS``
# above only ever fires for a table that doesn't exist yet, so it cannot by
# itself carry this change to a pre-existing database). ``ADD COLUMN IF NOT
# EXISTS`` is a no-op — never an error — on a database that already has the
# column, whether because ``_TABLES`` just created it fresh or because a
# previous ``init_schema`` call already ran this same statement; that's what
# makes this, like every other statement in this module, safe to re-run.
#
# ``text[]``, not a normalized child table or a Postgres enum array: the
# reasons a run escalated for are a small (<=7, DESIGN-pinned), append-only,
# always-read-with-the-parent-row list that is never queried independently
# of its run (contrast ``cases``/``kb_chunks``, which genuinely need their
# own primary keys and indexes) — a child table would add a join for every
# read this ticket's own consumers (``portal.service.compute_metrics``'s
# per-reason counts, the feed's ``escalation_reason``) do for no benefit.
# The set of valid values is validated at the application boundary by
# ``escalation.schemas.Reason`` (a ``Literal``, enforced by every
# ``EscalationCall``/``EscalationTrigger`` Pydantic model before a reason
# ever reaches this column) — the same boundary ``runs.route``/``runs.
# outcome``'s sibling ``text``/enum columns already rely on their own
# producers to respect, so this isn't a new trust boundary for the table.
_MIGRATIONS = """
ALTER TABLE runs ADD COLUMN IF NOT EXISTS reasons text[] NOT NULL DEFAULT '{}'::text[];
"""


def init_schema(conn: psycopg.Connection) -> None:
    """Create every pinned table/type if it does not already exist, then
    run every idempotent migration (``_MIGRATIONS``) so a database created
    before a later column existed is upgraded in place."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(_ENUM_STAGE)
        cur.execute(_ENUM_OUTCOME)
        cur.execute(_ENUM_DRAFT)
        cur.execute(_TABLES)
        cur.execute(_MIGRATIONS)
    conn.commit()
