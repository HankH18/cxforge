"""End-to-end proof of the SPEC R13 gap this ticket closes: the escalation
REASON(S) an actual graph run escalates for must be persisted on ``runs``
and surfaced correctly through the portal API — not just that some row with
a route/outcome lands in the table (``test_gate_integration.py`` already
proves that shape of thing for the gate).

This drives the REAL agent graph (``agent.graph.run_agent``, no
``escalation_decider`` override — see its own docstring: it defaults to a
real ``escalation.engine.EscalationEngine`` built from the same fake
``LLMClient`` passed in) with a message engineered to trip NO deterministic
hard rule (``escalation.rules``'s billing/human-request regexes), so the
classifier is what decides — with a canned, genuinely MULTI-reason verdict
(``frustration`` AND ``complexity``), the same multi-valued shape DESIGN's
``EscalationCall.reasons: list[Reason]`` allows and
``test_metrics.py::test_escalations_by_reason_counts`` already proves the
metrics side of.

The test then:
  1. Computes the EXPECTED decision independently, by calling
     ``escalation.engine.EscalationEngine.evaluate`` directly against the
     same ticket/conversation ``agent.nodes.decide`` would have seen — the
     actual ground truth of "what the escalation engine decided", not a
     value this test invents.
  2. Runs the real graph and reads ``runs.reasons`` straight out of
     Postgres, asserting it matches that expectation exactly (proves
     ``agent.nodes.act`` -> ``agent.store.record_run`` -> ``data.schema``'s
     column, the write side of the wiring).
  3. Hits the real ``GET /api/metrics`` and ``GET /api/feed`` endpoints and
     asserts both reflect the same two reasons (proves ``portal.service``'s
     read side too) — closing the loop DESIGN pins end to end, not just at
     the database.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent.graph import run_agent
from agent.schemas import Classification
from data import Case, get_case, get_connection
from data.seed import seed_all
from escalation.engine import EscalationEngine
from escalation.schemas import EscalationCall
from helpdesk.email_adapter import EmailAdapter

from .conftest import AUTH_HEADERS

CASE_ID = "MFG-2025-0734"

# Deliberately frustrated AND "this is complicated" language, with NO
# billing-dispute or explicit-human-request phrasing (escalation.rules'
# regexes) anywhere in it — the only thing that can escalate this run is
# the classifier's own verdict below, not a hard rule short-circuiting
# before the classifier is ever consulted (contrast
# ``backend/tests/graph/test_live_escalation_classifier.py``'s test 3,
# which proves the opposite case).
_FRUSTRATED_COMPLEX_MESSAGE = (
    f"This is the fourth time I have written in about case {CASE_ID} and "
    "every single time I get ignored. This whole process has turned into "
    "an absurdly complicated mess of conflicting updates and I am at the "
    "end of my patience with it."
)

# Genuinely multi-valued, per DESIGN's EscalationCall.reasons: list[Reason]
# — exactly the shape agent.store.record_run's docstring says a run can
# carry more than one reason for.
_MULTI_REASON_CALL = EscalationCall(
    escalate=True, reasons=["frustration", "complexity"], confidence=0.9
)


class _FakeLLMClient:
    """Minimal ``agent.llm.LLMClient`` double for the two structured-output
    calls this run makes: ``classify`` (routes to ``case_status``, a case
    that resolves cleanly so nothing OTHER than the escalation classifier
    can cause an escalation here), and ``agent.nodes.decide``'s
    unconditional ``EscalationEngine.evaluate`` -> ``run_classifier`` call.
    """

    def structured(
        self, schema: type[BaseModel], messages: list[dict[str, Any]], temperature: float = 0.0
    ) -> BaseModel:
        if schema is Classification:
            return Classification(
                topic="repeated frustrated follow-up, process called too complicated",
                route="case_status",
                case_id=CASE_ID,
                confidence=0.9,
            )
        if schema is EscalationCall:
            return _MULTI_REASON_CALL
        raise AssertionError(f"unexpected structured() call for {schema.__name__}")


def test_persisted_run_reasons_match_the_real_escalation_engines_decision(
    client: TestClient,
) -> None:
    seed_all()
    case = get_case(CASE_ID)
    assert isinstance(case, Case)

    port = EmailAdapter()
    ticket_id = port.seed_ticket(requester_email=case.requester_email)
    port.seed_comment(ticket_id, author="customer", text=_FRUSTRATED_COMPLEX_MESSAGE)

    llm = _FakeLLMClient()

    # -- ground truth: what the REAL engine actually decides for this exact
    # ticket/conversation, computed independently of the graph run below.
    engine = EscalationEngine(llm=llm)
    ticket = port.fetch_ticket(ticket_id)
    conversation = port.fetch_conversation(ticket_id)
    expected_decision = engine.evaluate(
        ticket=ticket, conversation=conversation, topic="", tool_results={}
    )
    expected_reasons = [t.reason for t in expected_decision.triggers]

    # Sanity on the ground truth itself: this run must actually escalate,
    # and for BOTH classifier reasons — otherwise the assertions below
    # would trivially pass on an empty/single-reason case and prove nothing
    # about multi-reason wiring.
    assert expected_decision.escalate is True
    assert expected_reasons == ["frustration", "complexity"]

    # -- drive the real graph (no escalation_decider override -> the real
    # EscalationEngine, per agent.graph.run_agent's own docstring).
    result = run_agent(ticket_id, port=port, llm=llm)
    assert result["route"] == "escalate"

    # 1. Storage: runs.reasons holds exactly the engine's own reasons, in
    # the same order, not a fake/placeholder value.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, reasons FROM runs WHERE ticket_id = %s", (ticket_id,)
        )
        row = cur.fetchone()
    assert row is not None
    outcome, persisted_reasons = row
    assert outcome == "escalated"
    assert persisted_reasons == expected_reasons

    # 2. GET /api/metrics: both reasons show up, each counted once, for
    # this one escalated run (portal.service.compute_metrics's own
    # docstring: a multi-reason run is counted under EVERY one of its
    # reasons).
    metrics_response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert metrics_response.status_code == 200
    escalations_by_reason = metrics_response.json()["escalations_by_reason"]
    assert escalations_by_reason == {"frustration": 1, "complexity": 1}

    # 3. GET /api/feed: the feed's single escalation_reason field reports
    # both reasons for this run.
    feed_response = client.get("/api/feed", headers=AUTH_HEADERS)
    assert feed_response.status_code == 200
    feed_row = next(
        r for r in feed_response.json()["runs"] if r["ticket_id"] == ticket_id
    )
    assert feed_row["escalation_reason"] == "frustration, complexity"
