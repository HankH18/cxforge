"""``agent.nodes.decide``'s own wiring: the deterministic billing/
human_request sweep that runs for every run not already routed to
``"escalate"`` (see that function's docstring). Exercised end-to-end
through ``EscalationEngine`` (with a ``RefusingLLMClient`` — proving no
model call happens on this path) and ``agent.store`` monkeypatched so this
suite needs no Postgres connection.
"""

from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig

from agent import nodes, store, templates
from agent.nodes import AgentDeps
from agent.state import RunState
from escalation.engine import EscalationEngine

from .conftest import make_conversation, make_ticket
from .fakes import RefusingLLMClient


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


def test_ordinary_message_with_no_hard_rule_leaves_the_route_untouched() -> None:
    config, llm = _config()
    state = _resolved_case_status_state("What's the status of my case?")
    result = nodes.decide(state, config)

    assert "route" not in result  # decide() did not override the route at all
    assert "escalation" not in result
    assert result["tool_results"]["decision"] == {"gate_enabled": False}
    assert llm.calls == 0


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
