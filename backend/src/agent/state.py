"""Graph state and routing literal — DESIGN §Agent graph, pinned verbatim:

    RunState(ticket, conversation, topic, route, tool_results,
    retrieved_chunks, draft, verifier_score, escalation, confidence, actions)

    Route = Literal["case_status", "permission", "kb", "off_topic", "escalate"]

The eleven field *names* above, and the five ``Route`` values, are pinned
and reproduced here exactly. DESIGN pins names, not per-field Python types,
so the type each field carries is this ticket's implementation choice,
documented per field below.

``ClassifyRoute`` and ``AlwaysGrantKind`` are this ticket's own additions
(not pinned by DESIGN) needed to keep two behaviors type-safe:

- The ``classify`` node must never itself decide ``"escalate"`` — DESIGN
  §Escalation contract makes the classifier-abstention-plus-threshold
  *judgment* T-6's job, so classify's structured-output schema is typed
  against the narrower ``ClassifyRoute`` (the four branch nodes only).
  ``route: Route`` on ``RunState`` still allows ``"escalate"`` because that
  value legitimately appears mid-run, once a branch node or ``verify``
  detects a condition it forwards to the escalation seam (see
  ``agent.escalation_seam``).
- ``AlwaysGrantKind`` is the closed, KB-grounded set of permission requests
  R3's always-grant policy covers (``fixtures/kb/case-information-authorization.md``).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from agent.escalation_seam import EscalationDecision
from data import RetrievedChunk
from helpdesk.models import Message, Ticket

Route = Literal["case_status", "permission", "kb", "off_topic", "escalate"]

ClassifyRoute = Literal["case_status", "permission", "kb", "off_topic"]

# The full always-granted list, verbatim from
# fixtures/kb/case-information-authorization.md's "The full always-granted
# list" section — the three request kinds R3 requires the agent to grant
# without escalation, once requester identity is confirmed.
AlwaysGrantKind = Literal["add_authorized_contact", "resend_report", "extend_retention"]


class RunState(TypedDict, total=False):
    ticket: Ticket
    """The Zendesk (or Email) ticket, freshly fetched by ``ingest`` every
    run — never carried over from a prior run (R7: stateless rebuild)."""

    conversation: list[Message]
    """The full comment thread, freshly fetched by ``ingest`` every run
    (R7). No server-side conversation memory: this list is the only
    context any node has about prior turns."""

    topic: str
    """``classify``'s short natural-language paraphrase of what the
    customer is asking — used as the KB retrieval query and folded into
    the escalation-note "conversation summary" (R6)."""

    route: Route | None
    """One of the four branch values while a run is in progress, or
    ``"escalate"`` once a branch/``verify`` has forwarded a detected
    condition to the escalation seam. Never written by ``classify`` as
    ``"escalate"`` directly — see the module docstring."""

    tool_results: dict[str, Any]
    """Grounding-step outputs, keyed by what produced them. Populated keys
    in this implementation: ``"case_id_hint"`` (case_id ``classify``
    extracted from the message, or ``None``), ``"case"`` (the resolved
    ``data.Case`` — case facts NEVER reach a reply except through this
    key, per R9), ``"permission_kind"`` (the matched ``AlwaysGrantKind``),
    ``"retrieved_policy_chunks"`` (grounding for a permission match), and
    ``"decision"`` (``decide``'s ``{"gate_enabled": bool}`` handoff to
    ``act``)."""

    retrieved_chunks: list[RetrievedChunk]
    """KB chunks ``kb_answer`` retrieved for a ``"kb"``-route question.
    ``verify`` scores the composed draft's groundedness against exactly
    these chunks — never against outside knowledge."""

    draft: str | None
    """The public-reply text as of the current node. Case facts reach this
    field ONLY via a template fed by ``tool_results["case"]`` fields
    (``agent.templates.render_case_status_reply``) — never free-generated.
    Free generation (an LLM call) is used only for the ``"kb"`` route's
    connective/answer prose, gated by ``verifier_score`` immediately after."""

    verifier_score: float | None
    """Groundedness score for a ``"kb"``-route draft, or ``None`` for every
    other route (nothing to score: case facts are templated, permission
    grants are a closed-list match, off-topic/escalation copy is fixed)."""

    escalation: EscalationDecision | None
    """Set once a T-5-detected condition (unresolvable/mismatched case,
    non-grantable permission, empty retrieval, sub-threshold verifier
    score) is forwarded to ``agent.escalation_seam.EscalationDecider``."""

    confidence: float | None
    """``classify``'s own confidence in its route choice. Not consulted by
    any T-5 hard-trigger check (that combinator is T-6's), but recorded on
    the ``runs`` row for T-7's eval work and T-8's portal feed (R10)."""

    actions: list[str]
    """Append-only trace of what each node did this run, in order — e.g.
    ``["ingest", "classify", "case_status", "compose", "verify", "decide",
    "act", "port:post_public_reply", ...]``. Graph tests assert against
    this to prove exactly which steps ran without over-fitting to internal
    node return shapes."""
