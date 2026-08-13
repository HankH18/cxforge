"""KB retrieval: pgvector cosine-distance search over ``kb_chunks``.

DESIGN pins case facts as never entering pgvector — retrieval here is KB
content only, structured case lookups stay in ``data.lookup``.
"""

from __future__ import annotations

from data.db import get_connection
from data.embeddings import Embedder, HashingEmbedder
from data.models import KBChunk, RetrievedChunk


def search_kb(
    query: str, k: int = 5, *, embedder: Embedder | None = None
) -> list[RetrievedChunk]:
    """Return the ``k`` KB chunks nearest ``query`` by cosine similarity.

    ``embedder`` must produce vectors comparable to those stored by the
    seeder (same implementation) — defaults to the same ``HashingEmbedder``
    ``seed_all`` uses.
    """
    resolved_embedder = embedder or HashingEmbedder()
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
    ]
