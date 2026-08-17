"""R6's ``empty_retrieval`` hard trigger, driven end-to-end for the first time.

``docs/STATE.md §6.4`` recorded this escalation path as **literally
unreachable**: ``agent.nodes.kb_answer`` escalates when ``search_kb`` comes
back empty, and ``search_kb`` applied no score cutoff, so nearest-neighbour
search always returned ``k`` chunks no matter how irrelevant the nearest
thing was. An escalation branch the docs described could never fire.

ADR-010 / BUILD-PLAN §1.3 added the relevance floor that makes it
reachable. These tests are the proof that it now fires *through the real
path* — the real seeded Postgres, the real ``search_kb``, the real default
embedder and its own calibrated floor, and the real graph. Nothing here
stubs retrieval; if it did, it would prove nothing at all about whether the
floor works, which is exactly the failure mode this project shipped 702
green tests on.

The question used is the body of ``esc-low_confidence-empty_retrieval-
accreditation-01`` from ``evals/labeled_set.yaml``, verbatim — a labeled
ticket that has always *claimed* to exercise this trigger and, until now,
could not. None of the 15 ``fixtures/kb/*.md`` documents covers lab
accreditation or international customs.
"""

from __future__ import annotations

import pytest

from agent import templates
from agent.graph import run_agent
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft
from data.retrieval import search_kb
from data.seed import SeedResult
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation
from .fakes import FakeLLMClient

pytestmark = pytest.mark.grounding

# evals/labeled_set.yaml :: esc-low_confidence-empty_retrieval-accreditation-01
UNCOVERED_QUESTION = (
    "Is Meridian ISO 17025 accredited, and what are your international "
    "shipping and customs requirements for skeletal remains?"
)
# What `kb_answer` actually embeds for that ticket — see COVERED_TOPIC below.
UNCOVERED_TOPIC = "asking about lab accreditation and international shipping"

# One of the 12 held-out natural-phrasing queries from
# backend/tests/data/test_retrieval.py — a question the KB genuinely answers.
COVERED_QUESTION = "how long until I hear back about my sample"

# `agent.nodes.kb_answer` searches with `state["topic"]` — the classifier's
# one-sentence paraphrase — falling back to the customer's message only when
# there is no topic. So the topic below is what actually gets embedded, and
# it is taken verbatim from `backend/tests/graph/test_canonical_scenarios.py`
# rather than invented here, so it cannot have been tuned to clear the floor.
#
# This indirection matters and was measured: under the offline lexical
# embedder a *vocabulary-free* paraphrase ("asking how long the process
# takes") scores 0.05 and retrieves nothing at all once the floor is on —
# 7 of 12 such paraphrases do. VoyageEmbedder retrieves for 12 of 12. See
# `HashingEmbedder.min_score` and the W2-B report; it is the strongest
# single argument for running production on Voyage.
COVERED_TOPIC = "sequencing turnaround time"


def test_retrieval_is_empty_for_a_question_the_kb_does_not_cover(seeded: SeedResult) -> None:
    """The precondition, asserted rather than assumed: with the default
    floor, this question retrieves nothing. Without the floor it retrieved
    five chunks about chain-of-custody, all irrelevant, top score 0.0761."""
    assert search_kb(UNCOVERED_QUESTION, k=5) == []
    assert search_kb(UNCOVERED_TOPIC, k=5) == []
    # And the floor is what does it — not an empty KB, not a broken query.
    assert len(search_kb(UNCOVERED_QUESTION, k=5, min_score=0.0)) == 5
    assert len(search_kb(UNCOVERED_TOPIC, k=5, min_score=0.0)) == 5


def test_empty_retrieval_escalates_the_run(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """The whole point of ADR-010: a kb-route question the KB cannot answer
    escalates instead of being answered from nothing.

    The ``FakeLLMClient`` deliberately registers **no** ``KBAnswerDraft``
    response. ``compose`` only asks for one on the ``"kb"`` route, and the
    fake raises ``AssertionError`` for an unregistered schema — so if the
    floor stopped working and retrieval returned chunks again, this test
    fails loudly on the way past rather than quietly asserting nothing.
    """
    ticket_id = seed_conversation(
        port, requester_email="curious@example.com", message=UNCOVERED_QUESTION
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic=UNCOVERED_TOPIC,
                route="kb",
                case_id=None,
                confidence=0.9,
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "escalate"
    assert result["retrieved_chunks"] == []
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY
    assert "kb_answer" in result["actions"]

    escalation = result["escalation"]
    assert escalation is not None
    assert escalation.escalate is True
    assert [(t.reason, t.detail) for t in escalation.triggers] == [
        ("low_confidence", "Empty KB retrieval for this question")
    ]

    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert len(notes) == 1
    assert "low_confidence" in notes[0].text
    assert "Empty KB retrieval" in notes[0].text


def test_a_covered_question_still_retrieves_and_answers_normally(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """The floor must not turn every kb question into an escalation.

    A floor that fires on everything is as broken as one that fires on
    nothing — and it would be an easy way to make the test above pass for
    the wrong reason. This drives the same real path with a question the KB
    does answer, and requires it to reach ``compose`` and send.
    """
    ticket_id = seed_conversation(
        port, requester_email="curious@example.com", message=COVERED_QUESTION
    )
    grounded_answer = (
        "Turnaround depends on which stage the work is in; our published "
        "per-stage windows are in the linked policy."
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic=COVERED_TOPIC,
                route="kb",
                case_id=None,
                confidence=0.9,
            ),
            KBAnswerDraft: KBAnswerDraft(answer=grounded_answer),
            GroundednessJudgment: GroundednessJudgment(
                score=0.95, rationale="Supported by the turnaround-times context."
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "kb"
    assert result["retrieved_chunks"], "a covered question must still retrieve chunks"
    assert result["draft"] == grounded_answer
    assert len(port.transport.sent) == 1
    assert port.transport.sent[0].html_body == grounded_answer
