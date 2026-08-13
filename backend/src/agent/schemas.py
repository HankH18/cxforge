"""Pydantic schemas passed to ``LLMClient.structured`` — one per model call
site in the graph. Kept separate from ``agent.nodes`` so
``backend/tests/graph/fakes.py``'s ``FakeLLMClient`` (keyed by schema
class) can import them without importing the graph's node functions.
"""

from __future__ import annotations

from pydantic import BaseModel

from agent.state import AlwaysGrantKind, ClassifyRoute


class Classification(BaseModel):
    """``classify`` node output. Never allowed to select ``"escalate"``
    itself — see ``agent.state``'s module docstring — so ``route`` is typed
    against the narrower ``ClassifyRoute``, not the full ``Route``."""

    topic: str
    route: ClassifyRoute
    case_id: str | None = None
    confidence: float


class PermissionMatch(BaseModel):
    """``permission`` node output: which (if any) of the closed,
    KB-grounded always-grant kinds the request matches. ``kind=None`` means
    "does not match the list" — the node escalates rather than guessing."""

    kind: AlwaysGrantKind | None


class KBAnswerDraft(BaseModel):
    """``compose``'s free-generated answer for the ``"kb"`` route — the one
    place DESIGN allows free generation, and only ever over the KB context
    handed to the model, never over case facts."""

    answer: str


class GroundednessJudgment(BaseModel):
    """``verify``'s groundedness score for a ``"kb"``-route draft against
    its retrieved chunks. A real judge call (possibly LLM-backed, possibly
    a fake in tests) — ``agent.nodes.verify`` genuinely gates on
    ``.score``, it does not merely record it."""

    score: float
    rationale: str
