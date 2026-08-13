"""Internal-note composition — SPEC R6 / DESIGN §Escalation contract: "post
an internal note (conversation summary, grounded facts, escalation
reason)." The three parts are rendered as clearly labeled, separated
sections so a human reviewer can tell at a glance which is which.

The GROUNDED FACTS section is template-filled from ``tool_results`` (and,
for a kb-route escalation, the run's ``retrieved_chunks``) ONLY — the same
R9 discipline T-5's ``agent.templates.render_case_status_reply`` already
enforces for public replies: every fact line below interpolates a field of
a real tool result, never free-generated prose about the case. The
CONVERSATION SUMMARY section is the one place DESIGN allows free text
("The summary may be LLM prose"); this implementation reuses ``classify``'s
own ``topic`` output (already LLM prose, already paid for) rather than
spending a second LLM call restating it — see ``agent.nodes.act``, the sole
caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.escalation_seam import EscalationTrigger
from data import Case, RetrievedChunk

_SUMMARY_HEADER = "=== CONVERSATION SUMMARY ==="
_FACTS_HEADER = "=== GROUNDED FACTS ==="
_REASONS_HEADER = "=== ESCALATION REASON(S) ==="


def _case_fact_lines(case: Case) -> list[str]:
    """Every line here interpolates a ``Case`` field verbatim — nothing
    free-generated, mirroring ``agent.templates.render_case_status_reply``."""
    lines = [
        f"Case on file: {case.case_id} (requester={case.requester_email})",
        f"  stage: {case.stage}",
        f"  stage_entered_at: {case.stage_entered_at.isoformat()}",
        f"  last_updated: {case.last_updated.isoformat()}",
    ]
    if case.eta_weeks is not None:
        lines.append(f"  eta_weeks: {case.eta_weeks}")
    if case.dna_profile_available is not None:
        lines.append(f"  dna_profile_available: {case.dna_profile_available}")
    if case.photos_available is not None:
        lines.append(f"  photos_available: {case.photos_available}")
    return lines


def _grounded_facts(
    tool_results: Mapping[str, Any], retrieved_chunks: Sequence[RetrievedChunk] | None
) -> list[str]:
    lines: list[str] = []

    case = tool_results.get("case")
    if isinstance(case, Case):
        lines.extend(_case_fact_lines(case))
    else:
        case_id_hint = tool_results.get("case_id_hint")
        if case_id_hint:
            # Describes an ABSENCE (what the customer referenced but this
            # run could not verify) — not an asserted fact, so it does not
            # violate R9's "never invent a case fact" discipline.
            lines.append(
                f"Case id referenced in the message (unresolved/unverified): {case_id_hint}"
            )
        else:
            lines.append("No case was resolved for this requester in this run.")

    permission_kind = tool_results.get("permission_kind")
    if permission_kind is not None:
        lines.append(f"Matched always-grant permission kind: {permission_kind}")

    policy_chunks = tool_results.get("retrieved_policy_chunks") or []
    if policy_chunks:
        slugs = ", ".join(sorted({c.chunk.doc_slug for c in policy_chunks}))
        lines.append(f"Retrieved policy KB doc(s): {slugs}")

    if retrieved_chunks:
        slugs = ", ".join(sorted({c.chunk.doc_slug for c in retrieved_chunks}))
        lines.append(f"Retrieved KB doc(s) for this question: {slugs}")
    elif retrieved_chunks is not None:
        lines.append("KB retrieval returned no results for this question.")

    return lines


def compose_internal_note(
    *,
    topic: str,
    tool_results: Mapping[str, Any],
    triggers: Sequence[EscalationTrigger],
    retrieved_chunks: Sequence[RetrievedChunk] | None = None,
) -> str:
    """Render the escalation internal note's three required parts.

    ``triggers`` is the decision's own structured trigger list (never
    re-derived here — that is ``escalation.engine``'s job). The GROUNDED
    FACTS section reads only ``tool_results``/``retrieved_chunks`` fields,
    never ``topic`` or any other free-generated text; the two are kept in
    clearly separate, headed sections so a reader can tell which is which
    at a glance."""
    reasons = list(dict.fromkeys(t.reason for t in triggers))
    reason_line = ", ".join(reasons) if reasons else "unspecified"
    detail_lines = [f"- {t.reason}: {t.detail}" for t in triggers] or ["- (no detail recorded)"]

    facts = _grounded_facts(tool_results, retrieved_chunks)
    if not facts:
        facts = ["No case or KB grounding available for this run."]

    return "\n".join(
        [
            _SUMMARY_HEADER,
            topic or "(no summary available)",
            "",
            _FACTS_HEADER,
            *facts,
            "",
            _REASONS_HEADER,
            reason_line,
            *detail_lines,
        ]
    )
