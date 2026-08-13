"""The T-6 escalation-engine seam.

T-5's non-goal is explicit: "No escalation RULE logic beyond calling T-6's
interface (stub until T-6 lands)." DESIGN §Escalation contract pins the
*real* engine — hard rules (billing, explicit human request, out-of-
procedure, empty retrieval, verifier threshold, classifier abstention),
an ``EscalationCall(escalate, reasons, confidence)`` classifier, and a
confidence-threshold combinator — as T-6's scope, chosen on T-7's labeled
set.

But three of R6's hard triggers are things T-5's own graph nodes detect
directly, as a structural fact about what they just did, with no judgment
call involved:

- ``case_status``/``permission``: the case couldn't be resolved without
  guessing (missing, or on file for a different requester than the one
  asking) — DESIGN's "unknown/unresolvable case".
- ``permission``: the request doesn't match the closed, KB-grounded
  always-grant list — DESIGN's "out-of-procedure request".
- ``kb_answer``/``verify``: retrieval came back empty, or the composed
  draft's groundedness score is below ``config.VERIFIER_THRESHOLD`` —
  DESIGN's "empty retrieval" / "verifier_score < 0.7".

``EscalationDecider`` is the seam those detections are handed to, so the
*action* of escalating (post note, tag, assign group, tell the customer —
all in ``agent.nodes.act``) never has to know whether the decision came
from T-5's trivial detections or T-6's full engine.

``PlaceholderEscalationDecider`` is T-5's stand-in: it does no judgment of
its own — by construction it cannot, since implementing judgment here would
be exactly the escalation-rule logic T-5 is not scoped to write — it just
turns every trigger it's handed into an escalation. T-6's scope also
includes ``backend/src/agent/**``, so it replaces this one class with the
real engine (implementing the same ``EscalationDecider`` Protocol) and
wires it in via ``agent.graph.build_graph(..., escalation_decider=...)``.
No other graph code changes: every node that can detect a trigger already
calls ``escalation_decider.decide(...)`` instead of deciding for itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from typing import Any

    from helpdesk.models import Message, Ticket

# A strict subset of DESIGN §Escalation contract's `Reason` literal
# (`"billing","human_request","unknown_case","out_of_procedure",
# "low_confidence","frustration","complexity"`). T-5 only ever produces
# these three — the ones its own nodes structurally detect, listed in the
# module docstring above. The rest (billing, human_request, frustration,
# complexity) require judgment T-5 does not implement; T-6's real engine
# can still emit the full set.
TriggerReason = Literal["unknown_case", "out_of_procedure", "low_confidence"]


class EscalationTrigger(BaseModel):
    """One T-5-detected condition, handed to the decider."""

    reason: TriggerReason
    detail: str  # human-readable — folded into the internal escalation note


class EscalationDecision(BaseModel):
    """What the decider decided. ``escalate`` is always ``True`` coming out
    of ``PlaceholderEscalationDecider`` (T-5 only ever calls ``decide`` when
    it has already concluded escalation is necessary) — the field still
    exists as ``bool`` rather than being dropped, because T-6's real engine
    is allowed to veto/adjust per DESIGN's confidence-threshold combinator,
    and every caller (``agent.nodes``) already branches on this value
    rather than assuming ``True``."""

    escalate: bool
    triggers: list[EscalationTrigger]


class EscalationDecider(Protocol):
    def decide(
        self,
        *,
        trigger: EscalationTrigger,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision: ...


class PlaceholderEscalationDecider:
    """T-5's trivial stand-in — see module docstring. Escalates on every
    trigger handed to it; ignores ``ticket``/``conversation``/``topic``/
    ``tool_results`` entirely (a real hard-rule/classifier engine needs
    them; a pass-through does not). T-6 replaces this class, not the
    ``EscalationDecider`` Protocol it implements."""

    def decide(
        self,
        *,
        trigger: EscalationTrigger,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision:
        return EscalationDecision(escalate=True, triggers=[trigger])
