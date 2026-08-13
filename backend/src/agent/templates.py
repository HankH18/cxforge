"""Template-fill functions and fixed copy — where R9 (the grounding
invariant) actually gets enforced.

``render_case_status_reply`` is THE function case facts reach a reply
through. It takes a ``data.Case`` (a tool result — the return value of
``data.get_case``/``data.get_cases_by_requester`` in this run, never a
cached/remembered one) and interpolates its fields into a fixed string —
there is no LLM call anywhere in this function, so there is no path for a
free-generated or invented case fact to appear here. ``agent.nodes.compose``
is the only caller, and only for ``route == "case_status"``.

``render_permission_grant_reply`` is the same idea for R3: the granted
action name comes from the closed ``AlwaysGrantKind`` the ``permission``
node matched (itself grounded in the KB's always-grant list — see
``agent.nodes.permission``), not from free text.

``OFF_TOPIC_REPLY`` and ``ESCALATION_CUSTOMER_REPLY`` are fixed copy with no
interpolation at all — the latter is SPEC R6's customer-facing escalation
notice, publicly told to every escalated ticket's requester regardless of
why it escalated, deliberately with zero customer-supplied content
interpolated into it (a red-team finding T-5 already fixed once; T-6 must
not reintroduce it). The escalation INTERNAL note (conversation summary,
template-filled grounded facts, escalation reason enum) is composed by
``escalation.notes.compose_internal_note`` instead — T-6's territory, kept
out of this module since it is not a *customer-facing* reply template.
"""

from __future__ import annotations

from agent.state import AlwaysGrantKind
from data import Case

_STAGE_DESCRIPTIONS: dict[str, str] = {
    "intake": "paperwork verification and accessioning",
    "extraction": "isolating DNA from the submitted material",
    "sequencing": "library prep, sequencing, and bioinformatic processing",
    "genealogy": "genealogy research against consumer genetic-genealogy databases",
    "complete": "finished — the final report has been generated",
}


def render_case_status_reply(case: Case) -> str:
    """Fill the case-status template from ``Case`` fields ONLY (R2, R9).

    Every sentence below either names a fixed stage description (a static
    lookup table, not a fact about *this* case) or interpolates one of
    ``case``'s own fields verbatim — nothing here is free-generated.
    """
    dna = "available" if case.dna_profile_available else "not yet available"
    photos = "available" if case.photos_available else "not yet available"
    stage_note = _STAGE_DESCRIPTIONS.get(case.stage, case.stage)

    if case.stage == "complete":
        eta_sentence = "Your case is complete, so there is no further processing time remaining."
    else:
        eta_sentence = f"We estimate about {case.eta_weeks} more week(s) in this stage."

    return (
        f"Thanks for checking in on case {case.case_id}.\n\n"
        f"Current stage: {case.stage} ({stage_note}).\n"
        f"Stage entered: {case.stage_entered_at.isoformat()}.\n"
        f"Last updated: {case.last_updated.isoformat()}.\n"
        f"{eta_sentence}\n"
        f"DNA profile: {dna}.\n"
        f"Accession photos: {photos}.\n\n"
        "Let us know if you have any other questions about this case."
    )


_ALWAYS_GRANT_DESCRIPTIONS: dict[AlwaysGrantKind, str] = {
    "add_authorized_contact": "adding a new authorized contact to this case",
    "resend_report": "resending a copy of your already-delivered report to the email on file",
    "extend_retention": "extending this case's records-retention window by up to 12 months",
}


def render_permission_grant_reply(kind: AlwaysGrantKind) -> str:
    """Fill the always-grant confirmation template from the matched
    ``AlwaysGrantKind`` (R3) — no case facts are stated here at all."""
    described = _ALWAYS_GRANT_DESCRIPTIONS[kind]
    return (
        f"Happy to help — {described} is something we can take care of "
        "directly, and it's now been processed for your case.\n\n"
        "Let us know if there's anything else we can do."
    )


OFF_TOPIC_REPLY = (
    "Thanks for reaching out! This channel is for questions about your "
    "Meridian Forensic Genomics case — status updates, turnaround times, "
    "report delivery, and related policies. I wasn't able to match your "
    "message to any of those, so I haven't taken any action on it. If you "
    "do have a case-related question, just reply here and I'll help."
)

ESCALATION_CUSTOMER_REPLY = (
    "Thanks for reaching out. This needs a closer look from one of our "
    "specialists, so I've routed it to our team and they'll follow up with "
    "you directly. We appreciate your patience."
)
