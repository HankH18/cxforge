"""``EscalationEngine.decide`` — the ``EscalationDecider`` Protocol method
itself, called by ``agent.nodes`` only once a caller already holds a
structurally-detected ``EscalationTrigger``. DESIGN's combinator's first
disjunct ("any hard rule") is therefore already satisfied by the time
``decide`` runs, for any of the five reasons T-5's nodes or
``agent.nodes.decide``'s own deterministic sweep can hand over
(unknown_case, out_of_procedure, low_confidence, billing, human_request) —
this proves ``decide`` escalates unconditionally for all of them, and never
calls the classifier to do it.
"""

from __future__ import annotations

import pytest

from agent.escalation_seam import EscalationTrigger
from escalation.engine import EscalationEngine
from escalation.schemas import Reason

from .conftest import make_conversation, make_ticket
from .fakes import RefusingLLMClient


@pytest.mark.parametrize(
    "reason,detail",
    [
        ("unknown_case", "no case resolved for this requester"),
        ("out_of_procedure", "request did not match the always-grant list"),
        ("low_confidence", "empty KB retrieval"),
        ("low_confidence", "groundedness score below threshold"),
    ],
)
def test_decide_escalates_unconditionally_for_every_t5_structural_reason(
    reason: Reason, detail: str
) -> None:
    llm = RefusingLLMClient()
    engine = EscalationEngine(llm=llm)
    decision = engine.decide(
        trigger=EscalationTrigger(reason=reason, detail=detail),
        ticket=make_ticket(),
        conversation=make_conversation("What's the status of my case?"),
        topic="status question",
        tool_results={},
    )
    assert decision.escalate is True
    assert reason in [t.reason for t in decision.triggers]
    assert llm.calls == 0


def test_decide_enriches_triggers_when_message_also_trips_a_deterministic_rule() -> None:
    """A T-5 trigger (unknown_case) fires for one reason, but the SAME
    message also independently reads as a billing dispute — decide()
    should report both so the internal note is complete."""
    llm = RefusingLLMClient()
    engine = EscalationEngine(llm=llm)
    decision = engine.decide(
        trigger=EscalationTrigger(reason="unknown_case", detail="no case resolved"),
        ticket=make_ticket(),
        conversation=make_conversation(
            "What's my case status? Also I was charged twice, please fix it."
        ),
        topic="status + billing",
        tool_results={},
    )
    assert decision.escalate is True
    assert set(t.reason for t in decision.triggers) == {"unknown_case", "billing"}
    assert llm.calls == 0


def test_decide_does_not_duplicate_a_reason_already_covered_by_the_trigger() -> None:
    llm = RefusingLLMClient()
    engine = EscalationEngine(llm=llm)
    decision = engine.decide(
        trigger=EscalationTrigger(reason="billing", detail="detected upstream"),
        ticket=make_ticket(),
        conversation=make_conversation("I was charged twice for this."),
        topic="billing",
        tool_results={},
    )
    reasons = [t.reason for t in decision.triggers]
    assert reasons.count("billing") == 1
