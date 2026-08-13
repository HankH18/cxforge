"""Adversarial: a customer message engineered to READ like a billing
dispute, paired with a classifier that says "fine, no escalation needed" —
the hard rule must still win. DESIGN's combinator is an OR: a fired hard
rule cannot be vetoed by the model's opinion, at any layer of this engine
(the full ``evaluate`` combinator, the Protocol's ``decide``, and
``agent.nodes.decide``'s own wiring into the live graph).
"""

from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig

from agent import nodes, store, templates
from agent.nodes import AgentDeps
from agent.state import RunState
from escalation.classifier import EscalationCall
from escalation.engine import EscalationEngine

from .conftest import make_conversation, make_ticket
from .fakes import FakeLLMClient

BILLING_DISPUTE_MESSAGE = (
    "I was charged twice for my extraction fee this month — this is a "
    "billing error and I want it fixed."
)

# A classifier that, if consulted, would say the opposite of what the hard
# rule concludes: "no escalation needed, this is fine."
_CLASSIFIER_SAYS_FINE = FakeLLMClient(
    responses={EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.95)}
)


def test_engine_evaluate_hard_rule_wins_even_though_classifier_would_say_fine() -> None:
    engine = EscalationEngine(llm=_CLASSIFIER_SAYS_FINE)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation(BILLING_DISPUTE_MESSAGE),
        topic="billing dispute, worded as a routine question",
        tool_results={},
    )
    assert decision.escalate is True
    assert "billing" in [t.reason for t in decision.triggers]
    # The classifier was never even consulted — the hard rule short-circuits
    # before evaluate() reaches the LLM call at all.
    assert _CLASSIFIER_SAYS_FINE.calls == []


@pytest.fixture(autouse=True)
def _stub_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "read_gate_enabled", lambda: False)


def _config(llm: FakeLLMClient) -> RunnableConfig:
    engine = EscalationEngine(llm=llm)
    deps = AgentDeps(port=None, llm=llm, escalation_decider=engine)  # type: ignore[arg-type]
    return {"configurable": {"ticket_id": "T-1", "deps": deps}}


def test_live_graph_decide_node_still_escalates_when_classifier_would_say_fine() -> None:
    """End to end through ``agent.nodes.decide``: even if the (never
    consulted, per ``escalation.engine.EscalationEngine.decide``'s
    short-circuit) classifier would have waved this through, the run must
    still escalate on the billing hard rule alone."""
    llm = FakeLLMClient(
        responses={EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.95)}
    )
    state: RunState = {
        "ticket": make_ticket(),
        "conversation": make_conversation(BILLING_DISPUTE_MESSAGE),
        "topic": "billing dispute, worded as a routine question",
        "route": "kb",
        "tool_results": {"case_id_hint": None},
        "retrieved_chunks": [],
        "draft": "Billing works in three gates...",
        "verifier_score": 0.95,  # would otherwise sail through verification
        "escalation": None,
        "confidence": 0.9,
        "actions": [],
    }
    result = nodes.decide(state, _config(llm))

    assert result["route"] == "escalate"
    assert "billing" in [t.reason for t in result["escalation"].triggers]
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY
    assert llm.calls == []  # hard rule alone decided it; classifier never asked
