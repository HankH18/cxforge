"""PUT /api/drafts/{id}, POST .../approve, POST .../reject — DESIGN §Portal
API, SPEC R11/R12.

Fixtures are built directly through ``agent.store.record_run``/
``record_draft`` — the exact functions ``agent.nodes.act`` calls when the
gate holds a draft pending — rather than running the full LangGraph
pipeline per case, so each test can pin one outcome/body combination
without a fake LLMClient. ``backend/tests/graph/test_gate.py`` already
proves T-5's own ``decide``/``act`` reads the gate and writes
``runs``/``drafts`` correctly end to end; ``test_gate_integration.py`` in
this suite re-proves the single pinned scenario (a REAL graph run held
pending, then approved through the portal) end to end. This module proves
every OTHER edit/approve/reject transition against rows shaped exactly
like the ones that path produces.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agent.store import record_draft, record_run
from data import get_connection

from ._fake_port import FakeHelpdeskPort
from .conftest import AUTH_HEADERS

TICKET_ID = "portal-test-ticket-drafts"


def _seed_pending_draft(*, body: str = "Original composed draft body.") -> tuple[int, int]:
    run_id = record_run(
        ticket_id=TICKET_ID,
        route="kb",
        confidence=0.9,
        outcome=None,
        verifier_score=0.95,
        trace_id="trace-drafts-1",
        received_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        replied_at=None,
    )
    draft_id = record_draft(run_id=run_id, body=body, status="pending")
    return run_id, draft_id


# -- edit -------------------------------------------------------------------


def test_edit_stores_edited_body_and_leaves_original_intact(client: TestClient) -> None:
    _run_id, draft_id = _seed_pending_draft(body="Original text.")

    response = client.put(
        f"/api/drafts/{draft_id}", json={"body": "Edited text."}, headers=AUTH_HEADERS
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["edited_body"] == "Edited text."
    assert payload["body"] == "Original text."

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT body, edited_body FROM drafts WHERE id = %s", (draft_id,))
        row = cur.fetchone()
    assert row == ("Original text.", "Edited text.")


def test_edit_missing_draft_is_404(client: TestClient) -> None:
    response = client.put("/api/drafts/999999", json={"body": "x"}, headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_edit_after_approve_is_rejected(client: TestClient, fake_port: FakeHelpdeskPort) -> None:
    _run_id, draft_id = _seed_pending_draft()
    approve_response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert approve_response.status_code == 200

    edit_response = client.put(
        f"/api/drafts/{draft_id}", json={"body": "too late"}, headers=AUTH_HEADERS
    )
    assert edit_response.status_code == 409


# -- approve ------------------------------------------------------------


def test_edit_then_approve_sends_the_edited_body_not_the_original(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    _run_id, draft_id = _seed_pending_draft(body="Original text.")

    edit_response = client.put(
        f"/api/drafts/{draft_id}", json={"body": "Edited text."}, headers=AUTH_HEADERS
    )
    assert edit_response.status_code == 200

    approve_response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert approve_response.status_code == 200
    payload = approve_response.json()
    assert payload["sent_body"] == "Edited text."
    assert payload["status"] == "approved"

    # The port received the EDITED body, not the original.
    assert fake_port.public_replies == [(TICKET_ID, "Edited text.")]

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM drafts WHERE id = %s", (draft_id,))
        assert cur.fetchone() == ("approved",)
        cur.execute("SELECT outcome, replied_at FROM runs WHERE id = %s", (_run_id,))
        row = cur.fetchone()
    assert row is not None
    outcome, replied_at = row
    assert outcome == "gated_sent"
    assert replied_at is not None


def test_approve_without_edit_sends_the_original_body(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    _run_id, draft_id = _seed_pending_draft(body="Original text, never edited.")

    response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["sent_body"] == "Original text, never edited."
    assert fake_port.public_replies == [(TICKET_ID, "Original text, never edited.")]


def test_approve_gate_held_draft_records_gated_sent_and_sends_exactly_once(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    """The pinned acceptance scenario's second half: a gate-held draft (row
    shape identical to what a real gate-ON run produces — see
    ``test_gate_integration.py`` for the run itself) sends nothing until
    approved, then the port receives exactly one public reply."""
    _run_id, draft_id = _seed_pending_draft()
    assert fake_port.public_replies == []  # nothing sent while pending

    response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert len(fake_port.public_replies) == 1


def test_approve_missing_draft_is_404(client: TestClient, fake_port: FakeHelpdeskPort) -> None:
    response = client.post("/api/drafts/999999/approve", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert fake_port.public_replies == []


def test_double_approve_is_rejected_with_no_second_send(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    _run_id, draft_id = _seed_pending_draft()

    first = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert first.status_code == 200
    assert len(fake_port.public_replies) == 1

    second = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert second.status_code == 409
    assert len(fake_port.public_replies) == 1  # unchanged — no second send

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT outcome FROM runs WHERE id = %s", (_run_id,))
        assert cur.fetchone() == ("gated_sent",)


def test_approving_a_rejected_draft_is_rejected_with_no_send(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    _run_id, draft_id = _seed_pending_draft()

    reject_response = client.post(f"/api/drafts/{draft_id}/reject", headers=AUTH_HEADERS)
    assert reject_response.status_code == 200

    approve_response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert approve_response.status_code == 409
    assert fake_port.public_replies == []


def test_approve_send_failure_leaves_draft_pending_and_retryable(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    """If the port raises, the transaction rolls back — the draft stays
    ``pending`` (not some half-sent state), so a second approve call can
    still succeed."""
    _run_id, draft_id = _seed_pending_draft()
    fake_port.fail_next_reply = True

    failed = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert failed.status_code == 502
    assert fake_port.public_replies == []

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM drafts WHERE id = %s", (draft_id,))
        assert cur.fetchone() == ("pending",)
        cur.execute("SELECT outcome FROM runs WHERE id = %s", (_run_id,))
        assert cur.fetchone() == (None,)

    retried = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
    assert retried.status_code == 200
    assert len(fake_port.public_replies) == 1


# -- reject -------------------------------------------------------------


def test_reject_sends_nothing_and_marks_rejected(
    client: TestClient, fake_port: FakeHelpdeskPort
) -> None:
    run_id, draft_id = _seed_pending_draft()

    response = client.post(f"/api/drafts/{draft_id}/reject", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert fake_port.public_replies == []

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM drafts WHERE id = %s", (draft_id,))
        assert cur.fetchone() == ("rejected",)
        cur.execute("SELECT outcome FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == ("rejected",)


def test_reject_missing_draft_is_404(client: TestClient) -> None:
    response = client.post("/api/drafts/999999/reject", headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_double_reject_is_rejected(client: TestClient, fake_port: FakeHelpdeskPort) -> None:
    _run_id, draft_id = _seed_pending_draft()

    first = client.post(f"/api/drafts/{draft_id}/reject", headers=AUTH_HEADERS)
    assert first.status_code == 200

    second = client.post(f"/api/drafts/{draft_id}/reject", headers=AUTH_HEADERS)
    assert second.status_code == 409
    assert fake_port.public_replies == []
