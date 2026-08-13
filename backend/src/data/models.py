"""Typed models for the case system and KB chunks.

``CaseNotFound`` is a dedicated, non-``None`` sentinel type: R9 (DESIGN
"Grounding invariant") forbids the agent inventing case facts, so a lookup
miss must be something the caller is forced to branch on by *type* rather
than a value that behaves like "no case, move along" (``None``) or blows up
the run (a bare exception). ``get_case`` returns ``Case | CaseNotFound`` —
callers must narrow before they can read a single field.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Stage = Literal["intake", "extraction", "sequencing", "genealogy", "complete"]

STAGES: tuple[Stage, ...] = ("intake", "extraction", "sequencing", "genealogy", "complete")

KBCategory = Literal["sop", "policy", "service"]


class Case(BaseModel):
    """A row from ``cases``, pinned in DESIGN §Case system.

    ``requester_email`` is not in DESIGN's column list, but the very next
    sentence there requires lookup *by* requester_email — the column is
    necessary to satisfy the pinned contract, not a redesign.
    """

    case_id: str
    requester_email: str
    requester_name: str | None = None
    stage: Stage
    stage_entered_at: date
    last_updated: date
    eta_weeks: int | None = None
    dna_profile_available: bool | None = None
    photos_available: bool | None = None


class CaseNotFound(BaseModel):
    """Typed miss for a case lookup.

    Never ``None``, never a raised exception — a dedicated type so
    ``isinstance`` narrowing is the only way to reach a ``Case`` field.
    """

    found: Literal[False] = False
    case_id: str | None = None
    requester_email: str | None = None


class KBChunk(BaseModel):
    """One chunk of a KB doc, as stored in ``kb_chunks``."""

    id: int
    doc_slug: str
    chunk_index: int
    text: str


class RetrievedChunk(BaseModel):
    """A KB chunk plus its similarity score for one retrieval query."""

    chunk: KBChunk
    score: float
