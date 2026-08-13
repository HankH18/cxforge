"""Deterministic hard-rule predicates — DESIGN §Escalation contract's
pinned list, reproduced exactly: billing terms, explicit human request,
unknown/unresolvable case, out-of-procedure request, empty retrieval,
verifier_score < 0.7, classifier abstention.

Every function here is pure Python: no LLM call, no network, no randomness
— a hard rule that depends on a model is not deterministic, so none of
these touch ``LLMClient`` at all (that is ``escalation.classifier``'s job,
and it only ever handles the two DESIGN explicitly assigns to the
classifier: frustration and complexity).

Three of DESIGN's seven hard-rule conditions — unknown/unresolvable case,
out-of-procedure request, and (empty retrieval OR verifier_score < 0.7) —
are DETECTED upstream, structurally, by T-5's own graph nodes (see
``agent.nodes``' module docstring and ``agent.escalation_seam``): by the
time this engine is asked to decide, T-5 has already done the deterministic
work of resolving a case, matching a permission kind, running retrieval, or
scoring groundedness, and hands the *result* down as an
``agent.escalation_seam.EscalationTrigger``. DESIGN's own pinned ``Reason``
literal reflects this: it has exactly one value, ``"low_confidence"``, for
BOTH "empty retrieval" and "verifier_score < 0.7" — there is no separate
reason to distinguish them, because T-5 already collapses both into the
same trigger reason before this engine ever sees it (see
``agent.nodes.kb_answer`` and ``agent.nodes.verify``). The three predicates
below for these conditions therefore take the upstream trigger's reason
string directly, rather than re-deriving a case/permission/retrieval result
themselves — that lookup is T-5's job, already done once; duplicating it
here would be a second, divergent implementation of the same decision, not
a defense-in-depth check.
"""

from __future__ import annotations

import re

from escalation.schemas import EscalationCall, Reason

# -- billing dispute ---------------------------------------------------------
#
# Scoped to genuine DISPUTE / monetary-adjustment-demand language, not
# billing terminology in general: fixtures/kb/billing-and-payment-terms.md
# is explicit that "straightforward, non-disputed billing questions are
# fine to answer... the line is disagreement or a request for a monetary
# adjustment — that always escalates." A bare "how does billing work?"
# question must stay answerable by the kb route; only language disputing a
# charge, or demanding money back/a credit, hard-escalates.
_BILLING_DISPUTE_RE = re.compile(
    r"charged\s+(me\s+)?twice"
    r"|double[\s-]charg"
    r"|duplicate\s+charge"
    r"|charged\s+(the\s+|me\s+)?(the\s+)?wrong\s+amount"
    r"|wrong\s+amount\s+charged"
    r"|charged\s+incorrectly"
    r"|billed\s+incorrectly"
    r"|billing\s+error"
    r"|billing\s+dispute"
    r"|disput\w*\s+(this|the|my)?\s*(charge|invoice|bill|payment)"
    r"|overcharg"
    r"|unauthorized\s+charge"
    r"|incorrect\s+charge"
    r"|(refund|reimburse)\s+me\b"
    r"|(give|issue|process|want)\s+(me\s+)?(a|my)\s+refund"
    r"|refund\s+(my|the)\s+\w+\s+fee",
    re.IGNORECASE,
)


def is_billing_dispute(message_text: str) -> bool:
    """DESIGN's "billing terms" hard rule (SPEC R6: "billing dispute"),
    scoped to actual disputes/adjustment demands — see module comment.
    Never fires on a bare mention of billing vocabulary, which the kb
    route can answer directly per
    ``fixtures/kb/billing-and-payment-terms.md``."""
    return bool(_BILLING_DISPUTE_RE.search(message_text))


# -- explicit human request ---------------------------------------------------
#
# Drawn from fixtures/kb/escalation-and-specialist-requests.md's own
# keyword list for exactly this category.
_HUMAN_REQUEST_RE = re.compile(
    r"(talk|speak)\s+(to|with)\s+(a\s+)?(real\s+)?(person|human|representative|someone)"
    r"|real\s+(person|human|representative)"
    r"|actual\s+(person|human)"
    r"|get\s+me\s+a\s+specialist"
    r"|this\s+is\s+a\s+bot"
    r"|not\s+a\s+bot"
    r"|(i\s+)?(need|want)\s+a\s+(real\s+)?(person|human)"
    r"|automated\s+system"
    r"|escalate\s+(this\s+)?to\s+a\s+human"
    r"|human\s+(agent|representative|specialist)"
    r"|customer\s+service\s+(rep|representative|agent)"
    r"|call\s+me\s+back",
    re.IGNORECASE,
)


def is_explicit_human_request(message_text: str) -> bool:
    """DESIGN's "explicit human request" hard rule."""
    return bool(_HUMAN_REQUEST_RE.search(message_text))


# -- the three T-5-structurally-detected hard rules ---------------------------


def is_unknown_case(upstream_reason: Reason | None) -> bool:
    """DESIGN's "unknown/unresolvable case" hard rule — already detected by
    ``agent.nodes.case_status``/``agent.nodes.permission`` (a case lookup
    miss, or a case on file for a different requester). This engine only
    asks whether that is the reason the upstream trigger names."""
    return upstream_reason == "unknown_case"


def is_out_of_procedure(upstream_reason: Reason | None) -> bool:
    """DESIGN's "out-of-procedure request" hard rule — already detected by
    ``agent.nodes.permission`` (the request did not match the closed,
    KB-grounded always-grant list)."""
    return upstream_reason == "out_of_procedure"


def is_low_confidence_trigger(upstream_reason: Reason | None) -> bool:
    """DESIGN's "empty retrieval" AND "verifier_score < 0.7" hard rules —
    both already detected upstream (``agent.nodes.kb_answer``'s empty
    ``search_kb`` result, or ``agent.nodes.verify``'s groundedness score /
    grounding-guard violation) and both forwarded under the SAME reason,
    ``"low_confidence"`` — DESIGN's pinned ``Reason`` literal has no
    separate value for the two (see module docstring), so there is nothing
    left for this predicate to distinguish between them on."""
    return upstream_reason == "low_confidence"


# -- classifier abstention -----------------------------------------------------


def is_classifier_abstention(call: EscalationCall | None) -> bool:
    """DESIGN's "classifier abstention" hard rule: the escalation
    classifier failed to return a usable, schema-valid verdict (a refusal,
    a truncated response, any exception the underlying ``LLMClient``
    raises) — ``escalation.classifier.run_classifier`` returns ``None`` for
    exactly this case. Escalating on ``None`` is itself deterministic
    control flow (no model judgment is trusted here), even though the
    condition being checked is the OUTCOME of a model call."""
    return call is None
