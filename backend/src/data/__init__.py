"""Data layer: case system, KB chunks, runs/drafts/settings.

T-1 owns the full Postgres schema pinned in DESIGN §Data models, including
tables later tickets write to (``runs`` — T-5/T-6; ``drafts``/``settings`` —
T-8): T-1 is the only ticket with ``backend/src/data/**`` in scope, so the
whole run/draft/settings schema is created now, up front, via
``init_schema``. Later tickets read/write those tables through
``get_connection`` without touching this package.
"""

from data.db import get_connection, get_dsn
from data.embeddings import (
    EMBEDDING_DIM,
    Embedder,
    HashingEmbedder,
    VoyageEmbedder,
    default_embedder,
    min_score_for,
)
from data.lookup import get_case, get_cases_by_requester
from data.models import Case, CaseNotFound, KBChunk, RetrievedChunk, Stage
from data.retrieval import search_kb
from data.schema import init_schema
from data.seed import SeedResult, seed_all

__all__ = [
    "EMBEDDING_DIM",
    "Case",
    "CaseNotFound",
    "Embedder",
    "HashingEmbedder",
    "KBChunk",
    "RetrievedChunk",
    "SeedResult",
    "Stage",
    "VoyageEmbedder",
    "default_embedder",
    "get_case",
    "get_cases_by_requester",
    "get_connection",
    "get_dsn",
    "init_schema",
    "min_score_for",
    "search_kb",
    "seed_all",
]
