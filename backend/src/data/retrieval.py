"""KB retrieval: pgvector cosine-distance search over ``kb_chunks``.

DESIGN pins case facts as never entering pgvector — retrieval here is KB
content only, structured case lookups stay in ``data.lookup``.

**The relevance floor (ADR-010 / BUILD-PLAN §1.3).** Without a cutoff this
function always returned ``k`` chunks for any query at all, however
irrelevant — nearest-neighbour search has no opinion about whether the
nearest thing is close. That made R6's ``empty_retrieval`` hard trigger
*literally unreachable* (``docs/STATE.md §6.4``): ``agent.nodes.kb_answer``
escalates when ``search_kb`` comes back empty, and it never did. Dropping
below-floor chunks is what makes that escalation path exist.

The floor's default is not a constant of retrieval — it belongs to whichever
embedding space produced the scores, so it is read off the resolved embedder
(``data.embeddings.min_score_for``). See ``agent.config.KB_MIN_SCORE``.
"""

from __future__ import annotations

from data.db import get_connection
from data.embeddings import Embedder, default_embedder, min_score_for
from data.models import KBChunk, RetrievedChunk


def search_kb(
    query: str,
    k: int = 5,
    *,
    embedder: Embedder | None = None,
    min_score: float | None = None,
) -> list[RetrievedChunk]:
    """Return up to ``k`` KB chunks nearest ``query`` by cosine similarity,
    dropping any that score below ``min_score``.

    ``embedder`` must produce vectors comparable to those stored by the
    seeder (same embedding space) — defaults to the same
    ``data.embeddings.default_embedder()`` ``seed_all`` uses, so the two
    halves cannot drift apart by accident. ``input_type="query"`` is the
    read-side hint; the seeder passes ``"document"``.

    ``min_score`` defaults to the resolved embedder's own calibrated floor
    (``agent.config.KB_MIN_SCORE`` for the configured production embedder).
    Passing ``0.0`` restores the pre-ADR-010 "always return something"
    behaviour explicitly, which is what a calibration harness wants and what
    no production caller should want.
    """
    resolved_embedder = embedder or default_embedder(input_type="query")
    resolved_min_score = min_score if min_score is not None else min_score_for(resolved_embedder)
    query_vector = resolved_embedder.embed([query])[0]

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, doc_slug, chunk_index, text, 1 - (embedding <=> %s::vector) AS score
            FROM kb_chunks
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vector, query_vector, k),
        )
        rows = cur.fetchall()

    return [
        RetrievedChunk(
            chunk=KBChunk(id=row[0], doc_slug=row[1], chunk_index=row[2], text=row[3]),
            score=row[4],
        )
        for row in rows
        if row[4] >= resolved_min_score
    ]
