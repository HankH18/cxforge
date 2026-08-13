"""R11's approval gate: ON holds every outbound public reply as a pending
draft and sends nothing; OFF (default) sends autonomously. Reuses the
case-status scenario (the richest one — real case facts in the draft) to
directly contrast the two."""

from __future__ import annotations

from agent.graph import run_agent
from agent.schemas import Classification
from data import Case, get_case, get_connection
from data.seed import SeedResult
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation, set_gate
from .fakes import FakeLLMClient

CASE_ID = "MFG-2025-0734"


def _run_case_status(port: EmailAdapter) -> tuple[str, Case]:
    case = get_case(CASE_ID)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"What's the status of case {CASE_ID}?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=CASE_ID, confidence=0.9
            ),
        }
    )
    run_agent(ticket_id, port=port, llm=llm)
    return ticket_id, case


def test_gate_on_holds_draft_pending_and_sends_nothing(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    set_gate(True)

    ticket_id, case = _run_case_status(port)

    # No port call at all — nothing sent, no tag, no status change.
    assert port.transport.sent == []
    thread = port._threads[ticket_id]
    assert thread.status != "solved"
    assert "case-status" not in thread.tags

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, body, run_id FROM drafts")
        draft_rows = cur.fetchall()
    assert len(draft_rows) == 1
    status, body, run_id = draft_rows[0]
    assert status == "pending"
    assert CASE_ID in body
    assert case.stage in body

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, outcome, route, replied_at FROM runs")
        run_rows = cur.fetchall()
    assert len(run_rows) == 1
    run_row_id, outcome, route, replied_at = run_rows[0]
    assert run_row_id == run_id
    assert outcome is None  # T-8's approval flow fills this in later
    assert route == "case_status"
    assert replied_at is None


def test_gate_off_sends_via_port(seeded: SeedResult, port: EmailAdapter) -> None:
    set_gate(False)

    ticket_id, case = _run_case_status(port)

    assert len(port.transport.sent) == 1
    assert CASE_ID in port.transport.sent[0].html_body
    thread = port._threads[ticket_id]
    assert thread.status == "solved"
    assert "case-status" in thread.tags

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM drafts")
        draft_rows = cur.fetchall()
    assert draft_rows == [("auto_sent",)]

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT outcome, replied_at FROM runs")
        run_rows = cur.fetchall()
    assert len(run_rows) == 1
    outcome, replied_at = run_rows[0]
    assert outcome == "auto_sent"
    assert replied_at is not None


def test_gate_defaults_off_when_no_setting_row_exists(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """R11: "OFF (default): autonomous send." No settings row at all (the
    _clean_run_tables fixture truncates it) must behave like gate OFF."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM settings")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 0

    ticket_id, _case = _run_case_status(port)

    assert len(port.transport.sent) == 1
    assert port._threads[ticket_id].status == "solved"
