"""T-1 acceptance 1: cases schema + idempotent seeder, ~30 cases, every stage.

Also covers acceptance 2's "chunked and embedded into kb_chunks" from the
seeding side (chunk-count invariants); retrieval quality is test_retrieval.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data.db import get_connection
from data.models import STAGES
from data.seed import DEFAULT_KB_DIR, SeedResult, seed_all

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)


def _row_counts() -> tuple[int, int]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cases")
        cases_row = cur.fetchone()
        assert cases_row is not None
        cur.execute("SELECT count(*) FROM kb_chunks")
        chunks_row = cur.fetchone()
        assert chunks_row is not None
    return cases_row[0], chunks_row[0]


def test_seed_all_is_idempotent(seeded: SeedResult) -> None:
    """Seeding twice yields identical counts and raises nothing, in-process
    and at the database level."""
    first = seed_all()
    first_db_counts = _row_counts()

    second = seed_all()
    second_db_counts = _row_counts()

    assert first == second
    assert first_db_counts == second_db_counts
    assert first.case_count > 0
    assert first.kb_chunk_count > 0


def test_seeds_roughly_thirty_cases_across_every_stage(seeded: SeedResult) -> None:
    result = seed_all()
    assert 25 <= result.case_count <= 40

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT stage FROM cases")
        seen_stages = {row[0] for row in cur.fetchall()}
    assert seen_stages == set(STAGES)


def test_seeds_kb_chunks_covering_every_doc(seeded: SeedResult) -> None:
    result = seed_all()
    expected_doc_count = len(list(Path(DEFAULT_KB_DIR).glob("*.md")))
    assert expected_doc_count > 0

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT doc_slug FROM kb_chunks")
        seen_slugs = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT embedding FROM kb_chunks LIMIT 1")
        sample_row = cur.fetchone()
        assert sample_row is not None
        sample_vector = sample_row[0]

    assert len(seen_slugs) == expected_doc_count
    assert result.kb_chunk_count >= expected_doc_count  # at least one chunk per doc
    assert sample_vector.dimensions() == 1024  # data.embeddings.EMBEDDING_DIM
