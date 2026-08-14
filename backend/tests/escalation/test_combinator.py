"""DESIGN §Escalation contract's pinned combinator, exercised through
``escalation.engine.EscalationEngine.evaluate`` (the full combinator, no
assumed upstream trigger): "Final decision = any hard rule OR (classifier
escalate AND confidence >= threshold)." Truth table: hard-rule-only,
classifier-above-threshold, classifier-below-threshold, both, neither —
plus classifier abstention as its own hard trigger.
"""

from __future__ import annotations

from escalation.classifier import EscalationCall
from escalation.engine import EscalationEngine

from .conftest import make_conversation, make_ticket
from .fakes import AbstainingLLMClient, FakeLLMClient, RefusingLLMClient

THRESHOLD = 0.6


def _engine(llm: object) -> EscalationEngine:
    return EscalationEngine(llm=llm, threshold=THRESHOLD)  # type: ignore[arg-type]


# -- 1. hard rule only (classifier never consulted) ------------------------


def test_hard_rule_only_escalates_without_calling_the_classifier() -> None:
    llm = RefusingLLMClient()
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("I was charged twice for my extraction fee."),
        topic="billing dispute",
        tool_results={},
    )
    assert decision.escalate is True
    assert [t.reason for t in decision.triggers] == ["billing"]
    assert llm.calls == 0


# -- 2. classifier above threshold, no hard rule ----------------------------


def test_classifier_above_threshold_escalates() -> None:
    llm = FakeLLMClient(
        responses={
            EscalationCall: EscalationCall(
                escalate=True, reasons=["frustration"], confidence=0.9
            )
        }
    )
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("This is the third time I've asked, still no answer!"),
        topic="repeated frustrated follow-up",
        tool_results={},
    )
    assert decision.escalate is True
    assert [t.reason for t in decision.triggers] == ["frustration"]


# -- 3. classifier below threshold, no hard rule -> no escalation ----------


def test_classifier_below_threshold_does_not_escalate() -> None:
    llm = FakeLLMClient(
        responses={
            EscalationCall: EscalationCall(
                escalate=True, reasons=["frustration"], confidence=0.4
            )
        }
    )
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("A bit annoyed but it's fine, just checking in."),
        topic="mild annoyance, low signal",
        tool_results={},
    )
    assert decision.escalate is False
    assert decision.triggers == []


def test_classifier_escalate_false_does_not_escalate_regardless_of_confidence() -> None:
    llm = FakeLLMClient(
        responses={EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.99)}
    )
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("How long does sequencing usually take?"),
        topic="routine question",
        tool_results={},
    )
    assert decision.escalate is False


# -- 4. both hard rule and classifier fire -----------------------------------


def test_hard_rule_and_classifier_both_firing_still_escalates_once() -> None:
    """A hard rule already decides the outcome, so the classifier is never
    even reached (see test 1) — this proves the SAME for a message that
    would ALSO have tripped the classifier, using a refusing LLM to prove
    it truly never got called."""
    llm = RefusingLLMClient()
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation(
            "I need to talk to a real person, this billing error is driving me crazy."
        ),
        topic="human request + billing dispute",
        tool_results={},
    )
    assert decision.escalate is True
    assert set(t.reason for t in decision.triggers) == {"billing", "human_request"}
    assert llm.calls == 0


# -- 5. neither hard rule nor classifier fire ------------------------------


def test_neither_hard_rule_nor_classifier_does_not_escalate() -> None:
    llm = FakeLLMClient(
        responses={EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.1)}
    )
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("How long does the sequencing stage usually take?"),
        topic="routine turnaround question",
        tool_results={},
    )
    assert decision.escalate is False
    assert decision.triggers == []


# -- classifier abstention is itself a hard escalation trigger (pinned) ----


def test_classifier_abstention_hard_escalates() -> None:
    # A genuine, absorbable model failure (not RefusingLLMClient's
    # programming-error tripwire, which T-18's narrowed except no longer
    # catches) -> run_classifier's except fires -> returns None -> abstention.
    llm = AbstainingLLMClient()
    engine = _engine(llm)
    decision = engine.evaluate(
        ticket=make_ticket(),
        conversation=make_conversation("What's going on with my case?"),
        topic="ambiguous, classifier fails to answer",
        tool_results={},
    )
    assert decision.escalate is True
    assert [t.reason for t in decision.triggers] == ["low_confidence"]
    assert "abstain" in decision.triggers[0].detail.lower()
