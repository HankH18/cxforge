"""An escalation stays visible to both escalation readers no matter what a
human later does to its draft.

The defect this pins, measured on the live droplet (run 7, 2026-08-16): the
run's Langfuse trace carried ``escalation_reasons: ["frustration"]``,
``GET /api/feed`` reported ``escalation_reason: null`` for it, and
``GET /api/metrics`` reported ``escalations_by_reason: {}`` — zero
escalations against one that had actually happened. Both readers selected on
``runs.outcome = 'escalated'``, and

  * ``agent.nodes.act``'s gate-ON branch never writes that value in the
    first place (it records ``outcome=None`` and holds the draft), and
  * ``portal.service.approve_draft`` / ``reject_draft`` overwrite ``outcome``
    with ``gated_sent`` / ``rejected`` when the reviewer decides.

So the erased escalations were exactly the ones a human had to touch —
touching them is what erased them — while the run kept counting in the
``human_avoidance_rate`` denominator. Half-visible: it dragged the headline
number down and contributed nothing to the R13 breakdown.

``runs.reasons`` is the durable record that a run escalated. It is written
once, by ``agent.store.record_run`` from ``agent.nodes._escalation_reasons``,
and never rewritten by the portal; it is non-empty if and only if the run
escalated (``agent.nodes.decide`` only sets ``state["escalation"]`` when the
combinator returned ``escalate=True``, and a branch node only sets it when it
forwarded a condition to the escalation seam). ``outcome`` records what
became of the *reply*. These tests assert the readers read the former.

Rows are built through ``agent.store.record_run``/``record_draft`` with the
exact argument values ``act``'s gate-ON branch passes, then reviewed through
the real ``POST /api/drafts/{id}/approve|reject`` endpoints — so the
``outcome`` overwrite under test is the production one, not a value this file
invents.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from agent.store import record_draft, record_run
from data import get_connection
from escalation.schemas import Reason

from ._fake_port import FakeHelpdeskPort
from .conftest import AUTH_HEADERS

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _seed_gated_run(
    *,
    ticket_id: str,
    route: str,
    reasons: list[Reason] | None,
) -> tuple[int, int]:
    """One run held at the gate: ``outcome=None``, ``replied_at=None``, a
    ``pending`` draft — literally the tuple ``agent.nodes.act`` writes when
    ``gate_enabled`` is true (``store.record_run(..., outcome=None,
    replied_at=None, reasons=_escalation_reasons(state))`` then
    ``store.record_draft(..., status="pending")``)."""
    run_id = record_run(
        ticket_id=ticket_id,
        route=route,
        confidence=0.9,
        outcome=None,
        verifier_score=0.9,
        trace_id=f"trace-{ticket_id}",
        received_at=_RECEIVED_AT,
        replied_at=None,
        reasons=reasons,
    )
    draft_id = record_draft(run_id=run_id, body="escalation notice", status="pending")
    return run_id, draft_id


def _stored_outcome(run_id: int) -> str | None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT outcome FROM runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    assert row is not None
    outcome: str | None = row[0]
    return outcome


def _feed_row(client: TestClient, ticket_id: str) -> dict[str, Any]:
    response = client.get("/api/feed", headers=AUTH_HEADERS)
    assert response.status_code == 200
    row: dict[str, Any] = next(
        r for r in response.json()["runs"] if r["ticket_id"] == ticket_id
    )
    return row


def _escalations_by_reason(client: TestClient) -> dict[str, int]:
    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
    breakdown: dict[str, int] = response.json()["escalations_by_reason"]
    return breakdown


def test_approving_an_escalations_draft_does_not_erase_the_escalation(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    """The live droplet's run 7, reproduced: escalated for ``frustration``,
    held at the gate, then approved by a human."""
    run_id, draft_id = _seed_gated_run(
        ticket_id="esc-approved", route="escalate", reasons=["frustration"]
    )

    approve = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert approve.status_code == 200
    assert fake_port.public_replies == [("esc-approved", "escalation notice")]

    # The overwrite this test exists for actually happened — otherwise the
    # assertions below would pass for the trivial reason that nothing
    # touched `outcome`.
    assert _stored_outcome(run_id) == "gated_sent"

    assert _escalations_by_reason(client) == {"frustration": 1}
    assert _feed_row(client, "esc-approved")["escalation_reason"] == "frustration"


def test_rejecting_an_escalations_draft_does_not_erase_the_escalation(
    client: TestClient,
) -> None:
    """``reject_draft`` overwrites ``outcome`` exactly as ``approve_draft``
    does — with ``'rejected'``, which is not even in
    ``_TERMINAL_OUTCOMES`` — so the reject path needs its own proof, not an
    argument by analogy from the approve one."""
    run_id, draft_id = _seed_gated_run(
        ticket_id="esc-rejected", route="escalate", reasons=["billing"]
    )

    reject = client.post(f"/api/drafts/{draft_id}/reject", headers=AUTH_HEADERS)
    assert reject.status_code == 200
    assert _stored_outcome(run_id) == "rejected"

    assert _escalations_by_reason(client) == {"billing": 1}
    assert _feed_row(client, "esc-rejected")["escalation_reason"] == "billing"


def test_a_multi_reason_escalation_keeps_every_reason_after_approval(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    """A run citing two reasons is counted under both after review, and the
    feed still comma-joins both — the multi-valued shape DESIGN's
    ``EscalationCall.reasons: list[Reason]`` allows, carried through the
    ``outcome`` overwrite."""
    _run_id, draft_id = _seed_gated_run(
        ticket_id="esc-multi", route="escalate", reasons=["frustration", "complexity"]
    )

    assert client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS).status_code == 200
    assert fake_port.public_replies

    assert _escalations_by_reason(client) == {"frustration": 1, "complexity": 1}
    assert _feed_row(client, "esc-multi")["escalation_reason"] == "frustration, complexity"


def test_an_escalation_held_at_the_gate_is_visible_before_anyone_reviews_it(
    client: TestClient,
) -> None:
    """The gate-ON branch of ``act`` records ``outcome=None``, so an
    escalation was invisible to both readers from the moment it was held —
    before any reviewer touched it. Approval is what made the erasure
    permanent, not what caused it."""
    run_id, _draft_id = _seed_gated_run(
        ticket_id="esc-held", route="escalate", reasons=["human_request"]
    )
    assert _stored_outcome(run_id) is None

    assert _escalations_by_reason(client) == {"human_request": 1}
    assert _feed_row(client, "esc-held")["escalation_reason"] == "human_request"


def test_reviewing_a_non_escalated_run_reports_no_escalation(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    """The other direction, and the one a careless fix breaks: a routine
    ``kb`` run held at the gate and approved carries no reasons, and must
    contribute nothing to either reader. Without this, "report every
    reviewed run as an escalation" would satisfy every assertion above."""
    run_id, draft_id = _seed_gated_run(ticket_id="esc-none", route="kb", reasons=None)

    assert client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS).status_code == 200
    assert fake_port.public_replies
    assert _stored_outcome(run_id) == "gated_sent"

    assert _escalations_by_reason(client) == {}
    assert _feed_row(client, "esc-none")["escalation_reason"] is None
