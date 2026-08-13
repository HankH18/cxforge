"""Internal-note composition (``escalation.notes.compose_internal_note``) —
SPEC R6 / DESIGN §Escalation contract: "post an internal note (conversation
summary, grounded facts, escalation reason)." Three required parts, clearly
separated; the GROUNDED FACTS section must match tool-result fields exactly
(template-filled, never free-generated).
"""

from __future__ import annotations

from agent.escalation_seam import EscalationTrigger
from data import KBChunk, RetrievedChunk
from escalation.notes import compose_internal_note

from .conftest import make_case


def test_note_contains_all_three_required_parts_clearly_separated() -> None:
    note = compose_internal_note(
        topic="asking about case status",
        tool_results={},
        triggers=[EscalationTrigger(reason="unknown_case", detail="no case resolved")],
    )
    assert "=== CONVERSATION SUMMARY ===" in note
    assert "=== GROUNDED FACTS ===" in note
    assert "=== ESCALATION REASON(S) ===" in note

    # Order: summary, then facts, then reasons — a reader can tell which
    # section they're in without cross-referencing.
    summary_idx = note.index("=== CONVERSATION SUMMARY ===")
    facts_idx = note.index("=== GROUNDED FACTS ===")
    reasons_idx = note.index("=== ESCALATION REASON(S) ===")
    assert summary_idx < facts_idx < reasons_idx


def test_note_summary_section_contains_the_topic_verbatim() -> None:
    note = compose_internal_note(
        topic="customer asking about a duplicate charge",
        tool_results={},
        triggers=[EscalationTrigger(reason="billing", detail="dispute detected")],
    )
    assert "customer asking about a duplicate charge" in note


def test_note_grounded_facts_match_resolved_case_fields_exactly() -> None:
    case = make_case(
        case_id="MFG-2025-0734",
        stage="extraction",
        eta_weeks=3,
        dna_profile_available=False,
        photos_available=True,
    )
    note = compose_internal_note(
        topic="status question",
        tool_results={"case": case},
        triggers=[EscalationTrigger(reason="low_confidence", detail="verifier failed")],
    )
    facts_section = note.split("=== GROUNDED FACTS ===")[1].split("=== ESCALATION REASON(S) ===")[
        0
    ]
    assert case.case_id in facts_section
    assert case.requester_email in facts_section
    assert f"stage: {case.stage}" in facts_section
    assert f"eta_weeks: {case.eta_weeks}" in facts_section
    assert f"dna_profile_available: {case.dna_profile_available}" in facts_section
    assert f"photos_available: {case.photos_available}" in facts_section
    assert case.last_updated.isoformat() in facts_section


def test_note_grounded_facts_never_include_topic_text() -> None:
    """The two sections stay genuinely separate: free-generated summary
    text must not leak into the template-filled facts section."""
    case = make_case()
    note = compose_internal_note(
        topic="UNIQUE_SUMMARY_MARKER_TEXT",
        tool_results={"case": case},
        triggers=[EscalationTrigger(reason="low_confidence", detail="x")],
    )
    facts_section = note.split("=== GROUNDED FACTS ===")[1].split("=== ESCALATION REASON(S) ===")[
        0
    ]
    assert "UNIQUE_SUMMARY_MARKER_TEXT" not in facts_section


def test_note_grounded_facts_state_absence_when_no_case_resolved() -> None:
    note = compose_internal_note(
        topic="status question for unresolvable case",
        tool_results={"case_id_hint": "MFG-9999-9999"},
        triggers=[EscalationTrigger(reason="unknown_case", detail="no match")],
    )
    facts_section = note.split("=== GROUNDED FACTS ===")[1]
    assert "MFG-9999-9999" in facts_section
    assert "unresolved/unverified" in facts_section


def test_note_grounded_facts_include_permission_kind_when_present() -> None:
    note = compose_internal_note(
        topic="permission request",
        tool_results={"case": make_case(), "permission_kind": "resend_report"},
        triggers=[EscalationTrigger(reason="out_of_procedure", detail="x")],
    )
    facts_section = note.split("=== GROUNDED FACTS ===")[1]
    assert "resend_report" in facts_section


def test_note_grounded_facts_include_retrieved_kb_doc_slugs() -> None:
    chunk = RetrievedChunk(
        chunk=KBChunk(id=1, doc_slug="billing-and-payment-terms", chunk_index=0, text="..."),
        score=0.5,
    )
    note = compose_internal_note(
        topic="kb question",
        tool_results={},
        triggers=[EscalationTrigger(reason="low_confidence", detail="x")],
        retrieved_chunks=[chunk],
    )
    facts_section = note.split("=== GROUNDED FACTS ===")[1]
    assert "billing-and-payment-terms" in facts_section


def test_note_reasons_section_lists_every_trigger_reason_and_detail() -> None:
    note = compose_internal_note(
        topic="x",
        tool_results={},
        triggers=[
            EscalationTrigger(reason="billing", detail="detail one"),
            EscalationTrigger(reason="human_request", detail="detail two"),
        ],
    )
    reasons_section = note.split("=== ESCALATION REASON(S) ===")[1]
    assert "billing" in reasons_section
    assert "human_request" in reasons_section
    assert "detail one" in reasons_section
    assert "detail two" in reasons_section
