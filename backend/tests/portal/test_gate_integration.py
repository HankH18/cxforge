"""One true end-to-end proof of the ticket's pinned acceptance scenario,
run through the REAL agent graph (T-5/T-6) into the portal's own approve
endpoint — not just fixture rows shaped like what that path produces (see
``test_drafts.py`` for the fixture-based coverage of every other
edit/approve/reject transition).

Mirrors ``backend/tests/graph/test_gate.py``'s own setup (a fake
``LLMClient``, ``EmailAdapter`` as the ``HelpdeskPort``, a seeded
case-status conversation, the gate written directly into ``settings``) —
that suite already proves T-5's ``decide``/``act`` reads the gate and
writes ``runs``/``drafts`` correctly; this test proves the SAME real run's
pending draft is exactly what T-8's ``/api/drafts/{id}/approve`` can pick
up and finish sending. Its ``FakeLLMClient``/``EscalationCall`` default
can't be imported from ``backend/tests/graph/fakes.py`` (that module lives
outside ``backend/tests/portal/**``, T-8's own file scope), so this file
defines its own minimal, self-contained equivalent.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent.config import GATE_SETTING_KEY
from agent.graph import run_agent
from agent.schemas import Classification
from data import Case, get_case, get_connection
from data.seed import seed_all
from escalation.schemas import EscalationCall
from helpdesk.email_adapter import EmailAdapter
from main import app
from portal.deps import get_helpdesk_port

from .conftest import AUTH_HEADERS

CASE_ID = "MFG-2025-0734"

_NON_ESCALATING_CALL = EscalationCall(escalate=False, reasons=[], confidence=0.0)


class _FakeLLMClient:
    """Minimal ``agent.llm.LLMClient`` double for exactly the two
    structured-output calls a case_status run makes: ``classify``, and
    ``agent.nodes.decide``'s unconditional escalation-classifier check
    (``escalation.engine.EscalationEngine.evaluate`` -> ``run_classifier``)
    — given a non-escalating default so this run reaches ``act`` normally.
    """

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        if schema is Classification:
            return Classification(
                topic="case status inquiry",
                route="case_status",
                case_id=CASE_ID,
                confidence=0.9,
            )
        if schema is EscalationCall:
            return _NON_ESCALATING_CALL
        raise AssertionError(f"unexpected structured() call for {schema.__name__}")


def _set_gate(enabled: bool) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (GATE_SETTING_KEY, "true" if enabled else "false"),
        )


def _seed_case_status_ticket(port: EmailAdapter, case: Case) -> str:
    ticket_id = port.seed_ticket(requester_email=case.requester_email)
    port.seed_comment(
        ticket_id, author="customer", text=f"What's the status of case {CASE_ID}?"
    )
    return ticket_id


def test_gate_on_real_run_holds_pending_then_portal_approve_sends_exactly_once(
    client: TestClient,
) -> None:
    seed_all()
    case = get_case(CASE_ID)
    assert isinstance(case, Case)

    port = EmailAdapter()
    app.dependency_overrides[get_helpdesk_port] = lambda: port
    try:
        _set_gate(True)
        ticket_id = _seed_case_status_ticket(port, case)

        run_agent(ticket_id, port=port, llm=_FakeLLMClient())

        # Gate ON: the real graph run held the draft pending and sent
        # nothing at all — re-asserted here as this test's own starting
        # point (backend/tests/graph/test_gate.py proves this in depth at
        # the graph level).
        assert port.transport.sent == []
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, status FROM drafts")
            draft_row = cur.fetchone()
        assert draft_row is not None
        draft_id, status = draft_row
        assert status == "pending"

        response = client.post(f"/api/drafts/{draft_id}/approve", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

        # The port receives EXACTLY one public reply.
        assert len(port.transport.sent) == 1
        assert CASE_ID in port.transport.sent[0].html_body

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT outcome FROM runs WHERE id = "
                "(SELECT run_id FROM drafts WHERE id = %s)",
                (draft_id,),
            )
            outcome_row = cur.fetchone()
        assert outcome_row is not None
        assert outcome_row[0] == "gated_sent"
    finally:
        app.dependency_overrides.pop(get_helpdesk_port, None)


def test_gate_off_real_run_records_auto_sent(client: TestClient) -> None:
    seed_all()
    case = get_case(CASE_ID)
    assert isinstance(case, Case)

    port = EmailAdapter()
    _set_gate(False)
    ticket_id = _seed_case_status_ticket(port, case)

    run_agent(ticket_id, port=port, llm=_FakeLLMClient())

    assert len(port.transport.sent) == 1
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT outcome FROM runs")
        outcome_row = cur.fetchone()
    assert outcome_row is not None
    assert outcome_row[0] == "auto_sent"
