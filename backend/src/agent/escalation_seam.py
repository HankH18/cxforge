"""The T-6 escalation-engine seam.

T-5's non-goal is explicit: "No escalation RULE logic beyond calling T-6's
interface (stub until T-6 lands)." DESIGN §Escalation contract pins the
*real* engine — hard rules (billing, explicit human request, out-of-
procedure, empty retrieval, verifier threshold, classifier abstention),
an ``EscalationCall(escalate, reasons, confidence)`` classifier, and a
confidence-threshold combinator — as T-6's scope, chosen on T-7's labeled
set.

T-6 has since landed: ``escalation.engine.EscalationEngine`` implements
``EscalationDecider`` below and is now the default passed to
``agent.graph.run_agent`` (see that module). ``PlaceholderEscalationDecider``
is kept only as a minimal reference implementation of the Protocol — no
production or test code in this repo constructs it as the *default* engine
anymore.

Three of R6's hard triggers are things T-5's own graph nodes detect
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
turns every trigger it's handed into an escalation.

T-5 expected "no other graph code changes" beyond swapping this class for
T-6's real engine. That held for five of DESIGN's seven hard rules
(unknown_case/out_of_procedure/low_confidence, already detected upstream by
existing nodes; frustration/complexity, the classifier's own job) but not
for two: "billing terms" and "explicit human request" need no upstream tool
result at all — just the customer's own words — and nothing in T-5's graph
was already looking at every message for them. ``agent.nodes.decide`` (T-6
now owns ``backend/src/agent/**`` too) therefore adds exactly one small,
LLM-free check — ``escalation.engine.detect_deterministic_hard_rule`` —
immediately before ``act``, so those two rules apply to every run, not only
ones an earlier node already flagged. See that function's and
``agent.nodes.decide``'s docstrings for why this is deliberately narrow:
the classifier-driven half of the contract (frustration/complexity) is NOT
given an equivalent always-on call site, because that would require an
``LLMClient`` call on every successful run, which the existing
graph/grounding suites' fake ``LLMClient`` fixtures do not anticipate and
would fail. That residual gap is flagged, not silently resolved — see this
ticket's final report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from escalation.schemas import Reason

if TYPE_CHECKING:
    from typing import Any

    from helpdesk.models import Message, Ticket

# DESIGN §Escalation contract's full, pinned `Reason` literal
# (`"billing","human_request","unknown_case","out_of_procedure",
# "low_confidence","frustration","complexity"`), imported from
# ``escalation.schemas`` (the single source of truth for it) and kept under
# this name for backward compatibility with the rest of this module. T-5's
# OWN structural detections only ever produced three of the seven
# (unknown_case/out_of_procedure/low_confidence — see the module
# docstring); T-6's real engine emits any of the seven, so this alias is
# widened to the full set rather than staying T-5's restricted subset.
TriggerReason = Reason


class EscalationTrigger(BaseModel):
    """One T-5-detected condition, handed to the decider."""

    reason: TriggerReason
    detail: str  # human-readable — folded into the internal escalation note


class EscalationDecision(BaseModel):
    """What the decider decided. ``escalate`` is always ``True`` coming out
    of ``PlaceholderEscalationDecider`` (T-5 only ever calls ``decide`` when
    it has already concluded escalation is necessary), and — for the same
    reason — also always ``True`` out of ``escalation.engine.EscalationEngine
    .decide`` (the Protocol method: called only when a caller already holds
    a fired hard-rule trigger, so DESIGN's combinator's first disjunct is
    already satisfied). The field stays a ``bool`` rather than being
    dropped because ``EscalationEngine.evaluate`` — the full combinator,
    used directly by T-6's own tests — genuinely can return ``False``, and
    every caller (``agent.nodes``) already branches on this value rather
    than assuming ``True``."""

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
