"""Idempotent seeder: loads ``fixtures/cases.yaml`` and ``fixtures/kb/*.md``.

Re-running never duplicates rows or raises. Both tables this seeder owns
(``cases``, ``kb_chunks``) are truncated and reloaded from the fixture files
on every call, so seeding is a pure function of current fixture content —
simpler and safer to reason about than an upsert, and there is no foreign
key into either table for a truncate to break.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
import yaml

from data.chunking import chunk_text, parse_kb_doc
from data.db import get_connection
from data.embeddings import Embedder, default_embedder
from data.models import Case
from data.schema import init_schema

# backend/src/data/seed.py -> data -> src -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "fixtures"
DEFAULT_CASES_PATH = FIXTURES_DIR / "cases.yaml"
DEFAULT_KB_DIR = FIXTURES_DIR / "kb"


@dataclass(frozen=True)
class SeedResult:
    case_count: int
    kb_chunk_count: int


def seed_all(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    kb_dir: Path = DEFAULT_KB_DIR,
    embedder: Embedder | None = None,
) -> SeedResult:
    """Create the schema if needed, then truncate-and-reload cases + KB chunks.

    ``embedder`` defaults to ``data.embeddings.default_embedder()`` — the
    same resolution ``data.retrieval.search_kb`` uses, so seeding and
    searching are always in the same embedding space. ``input_type
    ="document"`` is the index-side hint (see that module's docstring);
    ``search_kb`` passes ``"query"``.
    """
    resolved_embedder = embedder or default_embedder(input_type="document")
    with get_connection() as conn:
        init_schema(conn)
        case_count = _seed_cases(conn, cases_path)
        kb_chunk_count = _seed_kb(conn, kb_dir, resolved_embedder)
    return SeedResult(case_count=case_count, kb_chunk_count=kb_chunk_count)


def _seed_cases(conn: psycopg.Connection, cases_path: Path) -> int:
    payload = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    cases = [Case.model_validate(row) for row in payload.get("cases", [])]

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE cases")
        for case in cases:
            cur.execute(
                """
                INSERT INTO cases (
                    case_id, requester_email, requester_name, stage,
                    stage_entered_at, last_updated, eta_weeks,
                    dna_profile_available, photos_available
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    case.case_id,
                    case.requester_email,
                    case.requester_name,
                    case.stage,
                    case.stage_entered_at,
                    case.last_updated,
                    case.eta_weeks,
                    case.dna_profile_available,
                    case.photos_available,
                ),
            )
    return len(cases)


def _seed_kb(conn: psycopg.Connection, kb_dir: Path, embedder: Embedder) -> int:
    doc_paths = sorted(kb_dir.glob("*.md"))
    docs = [parse_kb_doc(p) for p in doc_paths]

    # (doc_slug, chunk_index, stored text, text actually embedded)
    rows: list[tuple[str, int, str, str]] = []
    for doc in docs:
        for idx, chunk in enumerate(chunk_text(doc.body)):
            # The title and curated keyword phrasings are folded into the
            # embedded text (not the stored text) so a query matching
            # doc-level vocabulary in the title, or a colloquial customer
            # phrasing that never appears in the prose at all ("how long
            # until I hear back" for a doc titled "Expected Turnaround
            # Times"), still ranks chunks from that doc highly — without
            # polluting every stored chunk's displayed text, which T-5's
            # templates and groundedness verifier read verbatim.
            keyword_text = "\n".join(doc.keywords)
            embed_text = f"{doc.title}\n{keyword_text}\n\n{chunk}"
            rows.append((doc.slug, idx, chunk, embed_text))

    embeddings = embedder.embed([r[3] for r in rows]) if rows else []

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE kb_chunks RESTART IDENTITY")
        for (doc_slug, chunk_index, text, _embed_text), vector in zip(
            rows, embeddings, strict=True
        ):
            cur.execute(
                "INSERT INTO kb_chunks (doc_slug, chunk_index, text, embedding) "
                "VALUES (%s, %s, %s, %s)",
                (doc_slug, chunk_index, text, vector),
            )
    return len(rows)
