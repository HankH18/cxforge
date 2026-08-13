"""Idempotent DDL for every table pinned in DESIGN §Data models.

T-1 is the only ticket with ``backend/src/data/**`` in scope, so every table
later tickets need — ``runs`` (T-5/T-6), ``drafts``/``settings`` (T-8) — is
created now, up front; those tickets only ever read/write rows through
``data.db.get_connection``, never alter the schema.

Every statement is re-runnable: ``CREATE TABLE IF NOT EXISTS``/``CREATE INDEX
IF NOT EXISTS`` for tables and indexes, a ``DO`` block guarding each enum
type against ``duplicate_object`` so re-running ``init_schema`` never errors.
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
    replied_at timestamptz
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


def init_schema(conn: psycopg.Connection) -> None:
    """Create every pinned table/type if it does not already exist."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(_ENUM_STAGE)
        cur.execute(_ENUM_OUTCOME)
        cur.execute(_ENUM_DRAFT)
        cur.execute(_TABLES)
    conn.commit()
