"""``agent.nodes.decide``'s own wiring: DESIGN's full escalation combinator
(hard rules, then — only if none fired — the classifier) for every run not
already routed to ``"escalate"`` (see that function's docstring). Hard-rule
cases are exercised through ``EscalationEngine`` with a ``RefusingLLMClient``
— proving the classifier is never reached once a hard rule already decided
the outcome; the no-hard-rule case is exercised with a ``FakeLLMClient``
registering a canned ``EscalationCall`` — proving the classifier genuinely
IS consulted there (see ``test_ordinary_message_no_hard_rule_consults_
classifier_leaves_route_untouched``'s own docstring for why this replaced
an earlier, now-incorrect version of that test).
``agent.store`` is monkeypatched so this suite needs no Postgres
connection.
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
from .fakes import FakeLLMClient, RefusingLLMClient


@pytest.fixture(autouse=True)
def _stub_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "read_gate_enabled", lambda: False)


def _config(*, ticket_id: str = "T-1") -> tuple[RunnableConfig, RefusingLLMClient]:
    llm = RefusingLLMClient()
    engine = EscalationEngine(llm=llm)
    deps = AgentDeps(port=None, llm=llm, escalation_decider=engine)  # type: ignore[arg-type]
    return {"configurable": {"ticket_id": ticket_id, "deps": deps}}, llm


def _resolved_case_status_state(message: str) -> RunState:
    return {
        "ticket": make_ticket(),
        "conversation": make_conversation(message),
        "topic": "case status inquiry",
        "route": "case_status",
        "tool_results": {"case_id_hint": None},
        "retrieved_chunks": [],
        "draft": "Thanks for checking in on case MFG-2025-0001.",
        "verifier_score": None,
        "escalation": None,
        "confidence": 0.9,
        "actions": [],
    }


def test_billing_dispute_flips_an_otherwise_resolving_route_to_escalate() -> None:
    config, llm = _config()
    state = _resolved_case_status_state(
        "What's my case status? Also I was charged twice for the extraction fee."
    )
    result = nodes.decide(state, config)

    assert result["route"] == "escalate"
    assert result["escalation"].escalate is True
    assert "billing" in [t.reason for t in result["escalation"].triggers]
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY
    assert llm.calls == 0  # deterministic hard rule alone decided this


def test_explicit_human_request_flips_an_otherwise_resolving_route_to_escalate() -> None:
    config, llm = _config()
    state = _resolved_case_status_state("Can I talk to a real person about my case, not a bot?")
    result = nodes.decide(state, config)

    assert result["route"] == "escalate"
    assert "human_request" in [t.reason for t in result["escalation"].triggers]
    assert llm.calls == 0


def test_ordinary_message_no_hard_rule_consults_classifier_leaves_route_untouched() -> None:
    """Replaces an earlier version of this test (pre-T-6-AC5-fix) that used
    a ``RefusingLLMClient`` and asserted ``llm.calls == 0`` here — i.e. that
    the classifier is NEVER consulted for an ordinary, no-hard-rule message.
    That assertion encoded the exact defect this ticket's final wiring
    fixes: DESIGN's pinned combinator ("any hard rule OR (classifier
    escalate AND confidence >= threshold)") requires the classifier's
    disjunct to be evaluated precisely when no hard rule has fired — see
    ``agent.nodes.decide``'s and ``agent.escalation_seam``'s docstrings.
    This version proves the corrected contract instead: the classifier IS
    called exactly once, and — since it reports no escalation — the route
    and escalation state are left untouched, same as before."""
    llm = FakeLLMClient(
        responses={EscalationCall: EscalationCall(escalate=False, reasons=[], confidence=0.1)}
    )
    engine = EscalationEngine(llm=llm)
    deps = AgentDeps(port=None, llm=llm, escalation_decider=engine)  # type: ignore[arg-type]
    config: RunnableConfig = {"configurable": {"ticket_id": "T-1", "deps": deps}}
    state = _resolved_case_status_state("What's the status of my case?")
    result = nodes.decide(state, config)

    assert "route" not in result  # decide() did not override the route at all
    assert "escalation" not in result
    assert result["tool_results"]["decision"] == {"gate_enabled": False}
    assert len(llm.calls) == 1
    assert llm.calls[0][0] is EscalationCall  # exactly the classifier, nothing else
    llm.assert_consulted(EscalationCall)


def test_already_escalated_route_is_not_re_swept() -> None:
    """If an earlier node already routed to escalate, decide() must not
    re-run the deterministic sweep (nothing to gain, and it would be
    redundant with EscalationEngine.decide's own re-check)."""
    config, llm = _config()
    state = _resolved_case_status_state("What's my case status?")
    state["route"] = "escalate"
    result = nodes.decide(state, config)

    assert "escalation" not in result  # decide() did not touch escalation itself
    assert llm.calls == 0
