"""End-to-end, in-process coverage of the pinned pipeline's canonical
scenarios (T-5 ticket acceptance #6): a resolving case-status question, a
permission request under the always-grant policy, a complex/technical
question that fails the groundedness verifier, and an off-topic message.

Real Postgres (case/KB data), a fake ``LLMClient``, and ``EmailAdapter``
reused as an in-memory ``HelpdeskPort`` recorder — see ``conftest.py``.
"""

from __future__ import annotations

from agent import templates
from agent.graph import run_agent
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft, PermissionMatch
from data import Case, get_case
from data.seed import SeedResult
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation
from .fakes import FakeLLMClient

# --- (1) case-status question that resolves --------------------------------


def test_case_status_question_resolves_to_public_reply_with_real_case_facts(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)

    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"Hi, can you tell me the current status of case {case_id}?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry",
                route="case_status",
                case_id=case_id,
                confidence=0.97,
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "case_status"
    assert result["escalation"] is None
    draft = result["draft"]
    assert draft is not None
    assert case_id in draft
    assert case.stage in draft
    assert str(case.eta_weeks) in draft

    # Exactly one public reply was sent, and it carries the real facts.
    assert len(port.transport.sent) == 1
    sent = port.transport.sent[0]
    assert sent.ticket_id == ticket_id
    assert case_id in sent.html_body
    assert case.stage in sent.html_body

    thread = port._threads[ticket_id]
    assert "case-status" in thread.tags
    assert thread.status == "solved"
    # No internal note, no escalation group — this never escalated.
    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert notes == []


# --- (2) permission request under always-grant -----------------------------


def test_permission_request_under_always_grant_is_granted_and_solved(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    case_id = "MFG-2025-0810"
    case = get_case(case_id)
    assert isinstance(case, Case)

    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message="Can you add my spouse as an authorized contact on my case?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="add an authorized contact",
                route="permission",
                case_id=None,
                confidence=0.91,
            ),
            PermissionMatch: PermissionMatch(kind="add_authorized_contact"),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "permission"
    assert result["escalation"] is None
    assert len(port.transport.sent) == 1

    thread = port._threads[ticket_id]
    assert thread.status == "solved"

    # A permission grant states no case fact at all (R9 doesn't need to be
    # invoked here — the template has nothing case-specific to leak).
    assert case.stage not in port.transport.sent[0].html_body
    assert str(case.eta_weeks) not in port.transport.sent[0].html_body


# --- (3) complex/technical question -> escalation ---------------------------


def test_complex_technical_question_fails_verifier_and_escalates(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    ticket_id = seed_conversation(
        port,
        requester_email="researcher@example.com",
        message=(
            "What exact demineralization chemistry do you use on degraded "
            "skeletal extracts, and how does that interact with heteroplasmy "
            "rates in downstream mtDNA variant calls?"
        ),
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="advanced extraction/sequencing chemistry question",
                route="kb",
                case_id=None,
                confidence=0.55,
            ),
            KBAnswerDraft: KBAnswerDraft(
                answer="Detailed chemistry specifics the KB doesn't actually cover."
            ),
            GroundednessJudgment: GroundednessJudgment(
                score=0.2, rationale="Draft makes claims the KB context does not support."
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "escalate"
    assert result["verifier_score"] == 0.2
    # The failed, unverified KB draft must never reach the customer — the
    # sent body is the fixed escalation redirect, not the LLM's answer.
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY

    assert len(port.transport.sent) == 1
    assert port.transport.sent[0].html_body == templates.ESCALATION_CUSTOMER_REPLY
    assert "heteroplasmy" not in port.transport.sent[0].html_body

    thread = port._threads[ticket_id]
    assert thread.status == "open"
    assert "escalated" in thread.tags
    assert thread.group_id is not None

    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert len(notes) == 1
    assert "low_confidence" in notes[0].text


def test_kb_question_that_passes_verifier_answers_and_solves(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """The mirror image of the scenario above, proving `verify` genuinely
    gates on the score both ways rather than always escalating kb drafts."""
    ticket_id = seed_conversation(
        port,
        requester_email="curious@example.com",
        message="How long does the sequencing stage usually take?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="sequencing turnaround time",
                route="kb",
                case_id=None,
                confidence=0.9,
            ),
            KBAnswerDraft: KBAnswerDraft(
                answer="Sequencing typically takes 3-8 weeks (commonly 4-6)."
            ),
            GroundednessJudgment: GroundednessJudgment(
                score=0.95, rationale="Matches the turnaround-times.md window exactly."
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "kb"
    assert result["verifier_score"] == 0.95
    assert result["draft"] == "Sequencing typically takes 3-8 weeks (commonly 4-6)."

    thread = port._threads[ticket_id]
    assert thread.status == "solved"
    assert "kb-answer" in thread.tags


# --- (4) off-topic -----------------------------------------------------------


def test_off_topic_message_gets_polite_redirect_and_stays_open(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    ticket_id = seed_conversation(
        port, requester_email="someone@example.com", message="Do you sell dog food?"
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="unrelated to the lab's services",
                route="off_topic",
                case_id=None,
                confidence=0.98,
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "off_topic"
    assert result["draft"] == templates.OFF_TOPIC_REPLY

    assert len(port.transport.sent) == 1
    assert port.transport.sent[0].html_body == templates.OFF_TOPIC_REPLY

    thread = port._threads[ticket_id]
    assert "off-topic" in thread.tags
    # R5: left open, never marked solved.
    assert thread.status == "open"
    assert thread.status != "solved"
