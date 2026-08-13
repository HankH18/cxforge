"""GET /api/feed?status= — DESIGN §Portal API / R10: "a portal feed of
every agent run: draft, sent body, route, confidence, escalation reason,
and a trace link."
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agent.store import record_draft, record_run
from data import get_connection
from escalation.schemas import Reason

from .conftest import AUTH_HEADERS


def _seed(
    *,
    ticket_id: str,
    outcome: str | None,
    draft_status: str,
    route: str = "kb",
    reasons: list[Reason] | None = None,
) -> int:
    run_id = record_run(
        ticket_id=ticket_id,
        route=route,
        confidence=0.8,
        outcome=outcome,
        verifier_score=0.9,
        trace_id=f"trace-{ticket_id}",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        replied_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC) if outcome else None,
        reasons=reasons,
    )
    record_draft(run_id=run_id, body="draft body", status=draft_status)
    return run_id


def test_feed_lists_every_run_with_the_pinned_fields(client: TestClient) -> None:
    _seed(ticket_id="feed-pending", outcome=None, draft_status="pending")
    _seed(ticket_id="feed-auto", outcome="auto_sent", draft_status="auto_sent")
    # An escalated run always carries at least one reason in real use
    # (agent.nodes.act threads the full escalation decision's reasons) —
    # seeded here so this fixture matches that, not the pre-fix placeholder
    # which fabricated "unspecified" regardless of what was recorded.
    _seed(
        ticket_id="feed-escalated",
        outcome="escalated",
        draft_status="auto_sent",
        reasons=["out_of_procedure"],
    )

    response = client.get("/api/feed", headers=AUTH_HEADERS)
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert len(runs) == 3

    ticket_ids = {row["ticket_id"] for row in runs}
    assert ticket_ids == {"feed-pending", "feed-auto", "feed-escalated"}

    for row in runs:
        for field in (
            "run_id",
            "ticket_id",
            "route",
            "confidence",
            "outcome",
            "draft_body",
            "sent_body",
            "escalation_reason",
            "trace_url",
        ):
            assert field in row

    by_ticket = {row["ticket_id"]: row for row in runs}
    # Pending: nothing sent yet, no escalation reason.
    assert by_ticket["feed-pending"]["sent_body"] is None
    assert by_ticket["feed-pending"]["escalation_reason"] is None
    # Auto-sent: sent body populated, no escalation reason.
    assert by_ticket["feed-auto"]["sent_body"] == "draft body"
    assert by_ticket["feed-auto"]["escalation_reason"] is None
    # Escalated: sent body populated (the fixed escalation notice went
    # out), AND an escalation reason is reported.
    assert by_ticket["feed-escalated"]["sent_body"] == "draft body"
    assert by_ticket["feed-escalated"]["escalation_reason"] is not None


def test_feed_status_filter_matches_draft_status(client: TestClient) -> None:
    _seed(ticket_id="feed-filter-pending", outcome=None, draft_status="pending")
    _seed(ticket_id="feed-filter-approved", outcome="gated_sent", draft_status="approved")

    response = client.get("/api/feed", params={"status": "pending"}, headers=AUTH_HEADERS)
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [row["ticket_id"] for row in runs] == ["feed-filter-pending"]


def test_feed_edited_draft_reports_edited_body_as_sent_body(client: TestClient) -> None:
    run_id = record_run(
        ticket_id="feed-edited",
        route="kb",
        confidence=0.8,
        outcome="gated_sent",
        verifier_score=0.9,
        trace_id="trace-feed-edited",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        replied_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
    )
    with_edit_draft_id = record_draft(run_id=run_id, body="original", status="approved")

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE drafts SET edited_body = %s WHERE id = %s",
            ("edited version", with_edit_draft_id),
        )

    response = client.get("/api/feed", headers=AUTH_HEADERS)
    assert response.status_code == 200
    row = next(r for r in response.json()["runs"] if r["ticket_id"] == "feed-edited")
    assert row["draft_body"] == "original"
    assert row["edited_body"] == "edited version"
    assert row["sent_body"] == "edited version"
