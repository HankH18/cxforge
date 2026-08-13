"""Request/response models for the portal API.

DESIGN §Portal API pins each endpoint's PATH and one-line purpose, not its
exact JSON shape (contrast the ``runs``/``drafts``/``settings`` table
columns, which DESIGN §Data models pins verbatim) — except
``GET /api/settings/gate``'s ``{enabled: bool}`` and ``GET /api/metrics``'s
``{human_avoidance_rate, latency_p50_s, latency_p95_s,
escalations_by_reason}``, both reproduced exactly below. Every other model
here is a reasonable, undocumented-but-unambiguous shape for the fields
DESIGN does name (draft / sent body / route / confidence / reason /
trace_url).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

DraftStatus = Literal["pending", "approved", "rejected", "auto_sent"]
RunOutcome = Literal["auto_sent", "gated_sent", "rejected", "escalated", "off_topic"]


class FeedItem(BaseModel):
    """One ``runs`` row joined to its ``drafts`` row — R10's "feed of every
    agent run: draft, sent body, route, confidence, escalation reason, and
    a trace link."
    """

    run_id: int
    ticket_id: str
    route: str | None
    confidence: float | None
    outcome: RunOutcome | None
    draft_id: int | None
    draft_status: DraftStatus | None
    draft_body: str | None
    edited_body: str | None
    sent_body: str | None
    escalation_reason: str | None
    trace_url: str | None
    received_at: datetime | None
    replied_at: datetime | None


class FeedResponse(BaseModel):
    runs: list[FeedItem]


class DraftEditRequest(BaseModel):
    body: str


class DraftResponse(BaseModel):
    """Returned by edit/approve/reject — the draft's post-operation state.
    ``sent_body`` is populated only by approve (what actually went out the
    port); ``None`` for edit/reject, which never send anything."""

    draft_id: int
    run_id: int
    ticket_id: str
    status: DraftStatus
    body: str
    edited_body: str | None
    sent_body: str | None = None


class GateSetting(BaseModel):
    """``GET|PUT /api/settings/gate``, pinned verbatim in DESIGN."""

    enabled: bool


class MetricsResponse(BaseModel):
    """``GET /api/metrics``, pinned verbatim in DESIGN §Portal API /
    §Metric definitions."""

    human_avoidance_rate: float
    latency_p50_s: float
    latency_p95_s: float
    escalations_by_reason: dict[str, int]
    """SPEC R13's "escalation counts by reason", one key per
    ``escalation.schemas.Reason`` value that fired on at least one escalated
    run. A run escalated for more than one reason is counted under EVERY
    one of its reasons, not just one (DESIGN's ``EscalationCall.reasons`` is
    genuinely multi-valued, and a hard rule can independently co-fire
    alongside the classifier) — so ``sum(escalations_by_reason.values())``
    can be, and often is, GREATER than the number of escalated runs. That
    is correct, surprising as it looks: each bucket counts "how many
    escalated runs cited this reason", and a run citing two reasons
    genuinely belongs in both. See ``portal.service.compute_metrics`` for
    the query that produces this."""
