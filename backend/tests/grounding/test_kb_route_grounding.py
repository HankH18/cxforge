"""R9 on the ``"kb"`` route — the one route DESIGN allows free generation
over — adversarially. Before ``agent.grounding_guard`` existed, this route
had NO deterministic backstop at all: ``compose`` wrote
``draft = result.answer`` straight from an ``LLMClient`` call, and
``verify``'s only gate was a groundedness score produced by that SAME
``LLMClient``. A hostile or merely broken model can fabricate a specific,
checkable case fact in its "KB answer" prose and simultaneously self-score
that answer 1.0 — nothing upstream of ``agent.grounding_guard`` can tell a
genuinely grounded KB answer apart from a fabrication the judge rubber-
stamped, because both paths are "ask the LLM".

Every hostile scenario below drives the REAL graph (``agent.graph.run_agent``)
through ``route == "kb"`` with a ``FakeLLMClient`` that (a) returns a KB
answer asserting a case fact free generation must never produce, and (b)
scores that same answer's groundedness 1.0 — proving the guard's escalation
does not, and cannot, depend on the judge's opinion. One evasion class per
test, matching what a red-team pass found: a fabricated case id in a
non-canonical rendering, a spelled-out (not digit) ETA, an indirect/
paraphrased stage claim, and a prose DNA-availability claim. The last test
is the mirror image: a genuinely clean KB answer with no case-fact-shaped
content must still send normally — the guard must not cost the happy path.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import templates
from agent.graph import run_agent
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft
from data import Case, get_case
from data.seed import SeedResult
from escalation.classifier import EscalationCall
from helpdesk.email_adapter import EmailAdapter

from .conftest import assert_no_case_facts_present, seed_conversation
from .fakes import FakeLLMClient

pytestmark = pytest.mark.grounding

# The real case the red-team reproduction used: extraction stage, 3 weeks
# ETA — every hostile draft below fabricates something else entirely
# (genealogy stage, 2 weeks, "a dozen more weeks", DNA availability, ...)
# for this same requester, so a passing test proves the fabrication (not
# the true case data) was what got blocked.
_CASE_ID = "MFG-2025-0734"


def _real_case() -> Case:
    case = get_case(_CASE_ID)
    assert isinstance(case, Case)
    assert case.stage == "extraction"
    assert case.eta_weeks == 3
    return case


def _run_hostile_kb(port: EmailAdapter, *, fabricated_answer: str) -> tuple[str, str]:
    """Seed a KB-shaped question from the real case's requester, and drive
    the real graph with a hostile LLM that answers with `fabricated_answer`
    and self-scores it a perfect 1.0. Returns ``(ticket_id, sent_body)``."""
    case = _real_case()
    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message="When will my case be finished?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="asking when their case will be done",
                route="kb",
                case_id=None,
                confidence=0.9,
            ),
            KBAnswerDraft: KBAnswerDraft(answer=fabricated_answer),
            GroundednessJudgment: GroundednessJudgment(
                score=1.0, rationale="Looks fully supported to me."
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "escalate", (
        "the fabricated free-generated draft must be blocked and the run escalated, "
        f"regardless of the self-reported groundedness score of {result['verifier_score']!r}"
    )
    assert result["verifier_score"] == 1.0  # the judge really did rubber-stamp it
    assert result["draft"] == templates.ESCALATION_CUSTOMER_REPLY

    assert len(port.transport.sent) == 1
    sent_body = port.transport.sent[0].html_body
    # The fabrication must never reach the port under any circumstance —
    # only the fixed, fact-free escalation redirect is ever sent.
    assert sent_body == templates.ESCALATION_CUSTOMER_REPLY
    assert fabricated_answer not in sent_body

    notes = [m for m in port.fetch_conversation(ticket_id) if not m.public]
    assert len(notes) == 1
    assert "low_confidence" in notes[0].text

    return ticket_id, sent_body


# --- reproduction: the exact red-team scenario ------------------------------


def test_fabricated_stage_and_eta_blocked_even_at_verifier_score_1(
    seeded: SeedResult, port: EmailAdapter, fixture_cases: list[dict[str, Any]]
) -> None:
    """The red team's exact reproduction: the real case is at `extraction`
    with a 3-week ETA; the hostile draft claims `genealogy`/2 weeks in the
    template's own idiom, and self-scores 1.0. This is the base case FIX 1
    exists to close — see this module's docstring."""
    fabricated = (
        "Good news — your case is now in the genealogy stage, and we "
        "estimate about 2 more weeks in this stage before your final "
        "report is ready."
    )
    _ticket_id, sent_body = _run_hostile_kb(port, fabricated_answer=fabricated)
    assert_no_case_facts_present(sent_body, fixture_cases)
    assert "genealogy" not in sent_body.lower()
    assert "2" not in sent_body


# --- evasion classes the red team enumerated --------------------------------


def test_evasion_spelled_out_eta_is_still_caught(
    seeded: SeedResult, port: EmailAdapter, fixture_cases: list[dict[str, Any]]
) -> None:
    """A spelled-out (not digit) turnaround claim — the guard's ETA
    extractor must resolve word-numbers too, not just ``\\d+``."""
    fabricated = (
        "Thanks for checking in! Your case is progressing well, and we "
        "expect about a dozen more weeks before your final report is ready."
    )
    _ticket_id, sent_body = _run_hostile_kb(port, fabricated_answer=fabricated)
    assert_no_case_facts_present(sent_body, fixture_cases)
    assert "dozen" not in sent_body.lower()


def test_evasion_prose_dna_availability_claim_is_still_caught(
    seeded: SeedResult, port: EmailAdapter, fixture_cases: list[dict[str, Any]]
) -> None:
    """A DNA-availability claim stated in ordinary prose, not the template's
    fixed "DNA profile: available." sentence — the real case's DNA profile
    is NOT available (see ``_real_case``'s fixture data), so this is a
    fabrication regardless of the sentence shape it's stated in."""
    fabricated = (
        "Great news about your case — your DNA profile is now available "
        "for review, and the team is finishing up the rest of the workflow."
    )
    _ticket_id, sent_body = _run_hostile_kb(port, fabricated_answer=fabricated)
    assert_no_case_facts_present(sent_body, fixture_cases)
    assert "dna" not in sent_body.lower()


def test_evasion_indirect_stage_phrasing_is_still_caught(
    seeded: SeedResult, port: EmailAdapter, fixture_cases: list[dict[str, Any]]
) -> None:
    """A stage claim phrased as a paraphrase ("building your family tree")
    rather than the literal word "genealogy" — literal-word matching alone
    would miss this; the guard's curated indirect-phrase list must catch
    it."""
    fabricated = (
        "Happy to help with your case! We're currently building your "
        "family tree by comparing your sample against public consumer "
        "databases, so you're getting close to the finish line."
    )
    assert "genealogy" not in fabricated.lower()  # prove this is the indirect-phrase path
    _ticket_id, sent_body = _run_hostile_kb(port, fabricated_answer=fabricated)
    assert_no_case_facts_present(sent_body, fixture_cases)
    assert "family tree" not in sent_body.lower()


def test_evasion_non_canonical_case_id_format_is_still_caught(
    seeded: SeedResult, port: EmailAdapter, fixture_cases: list[dict[str, Any]]
) -> None:
    """The real case id, rendered lowercase with spaces instead of dashes —
    the canonical-only ``\\bMFG-\\d{4}-\\d{4}\\b`` pattern the red team broke
    would miss this; the guard's loose/normalized matcher must not."""
    mangled_id = _CASE_ID.lower().replace("-", " ")
    assert mangled_id != _CASE_ID
    fabricated = (
        f"Thanks for reaching out about case {mangled_id} — it's "
        "progressing normally and we'll have an update for you soon."
    )
    _ticket_id, sent_body = _run_hostile_kb(port, fabricated_answer=fabricated)
    assert_no_case_facts_present(sent_body, fixture_cases)
    assert mangled_id not in sent_body.lower()
    assert _CASE_ID not in sent_body


# --- the guard must not cost the happy path ---------------------------------


def test_legitimate_kb_answer_with_no_case_facts_still_sends_normally(
    seeded: SeedResult, port: EmailAdapter
) -> None:
    """A genuinely clean KB answer — general policy/process prose with no
    case-id, stage, ETA, or DNA/photo-availability claim about any specific
    case — must still be composed, verified, and sent exactly as before.
    The guard adds a backstop; it must not become a second, stricter
    groundedness gate that blocks ordinary KB content."""
    ticket_id = seed_conversation(
        port,
        requester_email="curious-customer@example.com",
        message="In general, how long does the extraction stage usually take?",
    )
    clean_answer = (
        "Extraction typically takes about 1 to 2 weeks: the lab isolates "
        "DNA from the submitted material before moving on to sequencing."
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="general extraction-stage turnaround question",
                route="kb",
                case_id=None,
                confidence=0.9,
            ),
            KBAnswerDraft: KBAnswerDraft(answer=clean_answer),
            GroundednessJudgment: GroundednessJudgment(
                score=0.9, rationale="Matches the pipeline-stages-overview.md content."
            ),
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    llm.assert_consulted(EscalationCall)
    assert result["route"] == "kb"
    assert result["escalation"] is None
    assert result["verifier_score"] == 0.9
    assert result["draft"] == clean_answer

    assert len(port.transport.sent) == 1
    assert port.transport.sent[0].html_body == clean_answer

    thread = port._threads[ticket_id]
    assert thread.status == "solved"
    assert "kb-answer" in thread.tags
