"""T-6 acceptance criterion 5, proven against the REAL graph
(``agent.graph.run_agent``), not just ``agent.nodes.decide`` in isolation
(that unit-level proof already lives in ``backend/tests/escalation/**``).

Before this file existed, the classifier half of SPEC R6 ("escalates when a
hard trigger fires OR when the classifier flags frustration/complexity
above threshold") was unreachable from a live run: nothing in
``agent.nodes`` ever called ``escalation.classifier.run_classifier`` unless
a T-5 structural trigger (unresolvable case, out-of-procedure request,
empty retrieval, failed verification) had already fired one — i.e. unless
the decision to escalate had already been made for an unrelated reason.
Driving the real graph with a furious customer message and no hard trigger
routed the ticket straight to a KB answer and closed it out; the escalation
classifier was never even called. ``agent.nodes.decide`` now calls
``EscalationDecider.evaluate`` (DESIGN's full combinator) unconditionally
for every run that reaches it with ``state["route"] != "escalate"`` — see
that function's docstring for exactly which routes that covers and why.

Test 1 below is the exact repro this ticket's investigation used, replayed
through the fixed graph: a furious customer, a route a hard rule would
never touch (``case_status``, fully resolvable), and a classifier that says
"escalate, confidence above threshold" — the run must now escalate, post
the internal note, tag/assign the escalation group, and send the fixed
customer notice. Test 2 is the mirror image: the classifier says
"escalate" but below threshold — DESIGN's combinator requires
``confidence >= threshold`` too, so this must NOT escalate, and the normal
templated reply must still be sent. Test 3 re-proves the ordering guarantee
(a fired hard rule always wins, the classifier is never even consulted) at
this same full-graph level, complementing the existing unit-level proof in
``backend/tests/escalation/test_adversarial.py``.
"""

from __future__ import annotations

from agent import templates
from agent.graph import run_agent
from agent.schemas import Classification
from data import Case, get_case
from data.seed import SeedResult
from escalation.classifier import EscalationCall
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation
from .fakes import FakeLLMClient

# -- 1. frustrated customer, no hard rule, classifier escalates above
#       threshold -> the run must escalate (the confirmed-defect repro,
#       replayed through the fixed graph) ---------------------------------


def test_frustrated_customer_no_hard_rule_classifier_above_threshold_escalates(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)

    # Frustration language only — deliberately avoids every billing-dispute
    # and explicit-human-request keyword (escalation.rules), so nothing
    # here can trip a hard rule; only the classifier can catch this.
    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=(
            f"This is the fourth time I have written in about case {case_id} "
            "and every single time I get ignored. I am absolutely furious "
            "and at the end of my patience with this whole process."
        ),
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="repeated frustrated follow-up on case status",
                route="case_status",
                case_id=case_id,
                confidence=0.9,
            ),
            EscalationCall: EscalationCall(
                escalate=True, reasons=["frustration"], confidence=0.9
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "escalate"
    decision = result["escalation"]
    assert decision is not None
    assert decision.escalate is True
    assert "frustration" in [t.reason for t in decision.triggers]
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY

    # Customer notice posted (exactly the fixed template, no interpolation).
    assert len(port.transport.sent) == 1
    assert port.transport.sent[0].html_body == templates.ESCALATION_CUSTOMER_REPLY

    # Internal note posted, tagged, assigned to the escalation group.
    thread = port._threads[ticket_id]
    assert thread.status == "open"
    assert "escalated" in thread.tags
    assert "frustration" in thread.tags
    assert thread.group_id is not None

    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert len(notes) == 1
    assert "frustration" in notes[0].text


# -- 2. mirror: classifier says escalate, but confidence is below
#       threshold -> must NOT escalate, normal reply sent ------------------


def test_frustrated_customer_no_hard_rule_classifier_below_threshold_does_not_escalate(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    case_id = "MFG-2025-0810"
    case = get_case(case_id)
    assert isinstance(case, Case)

    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"Just checking in again on case {case_id}, feeling a little impatient today.",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="mildly impatient case status check-in",
                route="case_status",
                case_id=case_id,
                confidence=0.9,
            ),
            # escalate=True but below CLASSIFIER_CONFIDENCE_THRESHOLD (0.5,
            # provisional — not tuned here, see escalation.config) — DESIGN's
            # combinator requires BOTH escalate=True AND confidence>=threshold.
            EscalationCall: EscalationCall(
                escalate=True, reasons=["frustration"], confidence=0.2
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "case_status"
    assert result["escalation"] is None
    draft = result["draft"]
    assert draft is not None
    assert case_id in draft

    assert len(port.transport.sent) == 1
    assert case_id in port.transport.sent[0].html_body

    thread = port._threads[ticket_id]
    assert thread.status == "solved"
    assert "case-status" in thread.tags
    assert "escalated" not in thread.tags

    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert notes == []  # no internal note — this never escalated


# -- 3. ordering guarantee, at the full-graph level: a fired hard rule
#       always wins, and the classifier is never even consulted -----------


def test_hard_rule_wins_over_classifier_through_the_full_live_graph(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """Complements ``backend/tests/escalation/test_adversarial.py``'s
    unit-level proof (calling ``agent.nodes.decide`` directly) with the
    same guarantee driven end to end through ``run_agent``: a message that
    reads as both an explicit human request AND something the classifier
    would wave through must still escalate on the hard rule alone, and the
    classifier's contrary opinion must never even be asked for.

    Uses a case that resolves cleanly (real case id, matching requester) so
    the ONLY thing that could trigger escalation is the human-request text
    itself, caught by ``agent.nodes.decide``'s own ``evaluate`` call — not
    an unrelated upstream ``unknown_case`` structural trigger, which would
    prove a different (already well-covered) thing."""
    case_id = "MFG-2025-0301"
    case = get_case(case_id)
    assert isinstance(case, Case)

    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"I need to talk to a real person about case {case_id}, not a bot.",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="explicit request for a human about a resolvable case",
                route="case_status",
                case_id=case_id,
                confidence=0.85,
            ),
            # If the classifier were ever consulted, it would say "fine,
            # nothing to escalate" — the hard rule must win before that
            # opinion is ever asked for.
            EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.99),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "escalate"
    decision = result["escalation"]
    assert decision is not None
    assert decision.escalate is True
    assert "human_request" in [t.reason for t in decision.triggers]
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY

    thread = port._threads[ticket_id]
    assert "escalated" in thread.tags
    assert thread.group_id is not None

    # The classifier was never even asked — evaluate() short-circuits on
    # the hard rule before run_classifier is reached.
    escalation_calls = [c for c in llm.calls if c[0] is EscalationCall]
    assert escalation_calls == []
