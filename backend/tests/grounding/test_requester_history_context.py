"""ADR-009 / W2-B4 — the requester's prior tickets actually reach the
classifier, through the real graph and the real port.

The contract suite (``backend/tests/contract/test_port_contract.py``) proves
both adapters can *answer* ``fetch_requester_history`` correctly. That is a
different claim from "the agent asks, and the answer lands in the prompt" —
this repo has already shipped one severed connection between components that
each tested green (``docs/STATE.md §2``), so the connection gets its own
test. Nothing here stubs ``classify`` or hand-builds a message list: the
assertions read the message the ``LLMClient`` was actually handed.
"""

from __future__ import annotations

import pytest

from agent.graph import run_agent
from agent.schemas import Classification
from data.seed import SeedResult
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation
from .fakes import FakeLLMClient

pytestmark = pytest.mark.grounding

REQUESTER = "repeat-customer@example.com"
STRANGER = "someone-else@example.com"


def _classifier_prompt(llm: FakeLLMClient) -> str:
    """The user message `classify` handed the LLM — read back from the
    recorded call, not reconstructed."""
    calls = [messages for schema, messages in llm.calls if schema is Classification]
    assert len(calls) == 1, f"expected exactly one classify call, got {len(calls)}"
    return str(calls[0][-1]["content"])


def _llm() -> FakeLLMClient:
    return FakeLLMClient(
        responses={
            Classification: Classification(
                topic="unrelated to the lab's services",
                route="off_topic",
                case_id=None,
                confidence=0.95,
            ),
        }
    )


def test_prior_tickets_reach_the_classifier_prompt(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """A repeat contact reads differently from a first-time asker — the
    whole point of ADR-009. The prior subjects must be in the prompt, and
    must be labelled as history rather than merged into the message being
    classified."""
    port.seed_ticket(requester_email=REQUESTER, subject="Refund for the failed swab")
    port.seed_ticket(requester_email=REQUESTER, subject="Still waiting, third email")
    ticket_id = seed_conversation(
        port, requester_email=REQUESTER, message="Do you sell forensic t-shirts?"
    )
    llm = _llm()

    result = run_agent(ticket_id, port=port, llm=llm)

    prompt = _classifier_prompt(llm)
    assert "Refund for the failed swab" in prompt
    assert "Still waiting, third email" in prompt
    assert "previous tickets" in prompt
    assert "NOT the message to classify" in prompt
    # The current message is still there, and still the thing being classified.
    assert "Do you sell forensic t-shirts?" in prompt
    assert result["route"] == "off_topic"


def test_history_is_carried_on_tool_results_for_the_escalation_seam(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """`classify` stashes what it fetched so a later escalation note shows a
    human the same history the classifier saw, without a second API call
    that could disagree with the first."""
    port.seed_ticket(requester_email=REQUESTER, subject="Refund for the failed swab")
    ticket_id = seed_conversation(
        port, requester_email=REQUESTER, message="Do you sell forensic t-shirts?"
    )

    result = run_agent(ticket_id, port=port, llm=_llm())

    history = result["tool_results"]["requester_history"]
    assert [summary.subject for summary in history] == ["Refund for the failed swab"]


def test_a_first_time_requester_gets_no_history_block(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """The common case must not pay for the feature: with no prior contact
    the prompt is exactly the conversation, with no empty "previous tickets"
    heading inviting the model to infer something from its absence."""
    ticket_id = seed_conversation(
        port, requester_email=REQUESTER, message="Do you sell forensic t-shirts?"
    )
    llm = _llm()

    run_agent(ticket_id, port=port, llm=llm)

    prompt = _classifier_prompt(llm)
    assert "previous tickets" not in prompt
    assert prompt == "Customer: Do you sell forensic t-shirts?"


def test_another_requesters_tickets_never_reach_the_prompt(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """A leak here would put one customer's subject lines into another
    customer's classification context — and from there into a Langfuse trace
    and an internal note. Checked through the real graph, not only at the
    adapter."""
    port.seed_ticket(requester_email=STRANGER, subject="Somebody else's private matter")
    ticket_id = seed_conversation(
        port, requester_email=REQUESTER, message="Do you sell forensic t-shirts?"
    )
    llm = _llm()

    run_agent(ticket_id, port=port, llm=llm)

    assert "Somebody else" not in _classifier_prompt(llm)
