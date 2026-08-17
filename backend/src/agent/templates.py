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


# ADR-020's qualifier. Two constraints shape it, and both are load-bearing:
#
# 1. It attaches ONLY to a reply that actually states a forward-looking
#    estimate. A completed case has no timeline left to qualify, and
#    stamping "subject to change" onto a statement of fact would be noise
#    that teaches customers to ignore the words when they do matter.
# 2. It introduces **zero new facts**. Not a number, not a stage name, not a
#    cause ("due to lab throughput"). `agent.grounding_guard` exists to catch
#    exactly that shape of claim on free-generated text, and R9 is this
#    project's headline property — a disclaimer that smuggled in a fact would
#    deserve to trip it. Every content word below is a hedge on a figure
#    already present in the sentence it qualifies.
_ETA_QUALIFIER = "an estimated timeline, and subject to change"


def render_case_status_reply(case: Case) -> str:
    """Fill the case-status template from ``Case`` fields ONLY (R2, R9).

    Every sentence below either names a fixed stage description (a static
    lookup table, not a fact about *this* case) or interpolates one of
    ``case``'s own fields verbatim — nothing here is free-generated.

    ADR-020: when this reply states an ETA it carries ``_ETA_QUALIFIER``.
    W1-E3 measured the live model routing *"can you tell me the EXACT
    calendar date my results will be ready?"* to ``case_status`` at 0.92
    confidence — correctly, the owner decided — which means the honest
    answer to an exact-date question is this template's week estimate, said
    without implying more precision than the lab has.
    """
    dna = "available" if case.dna_profile_available else "not yet available"
    photos = "available" if case.photos_available else "not yet available"
    stage_note = _STAGE_DESCRIPTIONS.get(case.stage, case.stage)

    if case.stage == "complete":
        eta_sentence = "Your case is complete, so there is no further processing time remaining."
    else:
        # "About N more week(s)" rather than "We estimate about N more
        # week(s)" only so the qualifier does not immediately repeat the
        # word "estimate". `agent.grounding_guard`'s ETA detector keys on
        # "N more week(s)" and the "more week"/"in this stage" personalizing
        # cues, all of which are preserved verbatim.
        eta_sentence = (
            f"About {case.eta_weeks} more week(s) to go in this stage — {_ETA_QUALIFIER}."
        )

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
    ``AlwaysGrantKind`` (R3) — no case facts are stated here at all.

    ADR-011: this used to end *"it's now been processed for your case"*,
    which asserted a completed side effect **the codebase performs nowhere**.
    What the ``permission`` node actually does is decide, grounded in the
    KB's always-grant list, that the request needs no specialist review — an
    approval, not an execution. The wording below claims the approval and
    stops there, and it says plainly that the change itself is applied by
    someone else, so a customer whose contact never gets added knows to
    chase it instead of assuming it is done.

    ADR-011 explicitly scopes this to wording: implementing the side effects
    is a data-layer change and is not in scope here.
    """
    described = _ALWAYS_GRANT_DESCRIPTIONS[kind]
    return (
        f"Happy to help — {described} is covered by our standard "
        "authorizations, so it doesn't need a specialist to review it. "
        "I've approved the request and recorded that approval here on your "
        "ticket.\n\n"
        "Our support team makes the change itself, so if you don't see it "
        "take effect, just reply here and we'll follow up."
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
