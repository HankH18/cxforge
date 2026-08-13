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
T-6's real engine. That held for three of DESIGN's seven hard rules
(unknown_case/out_of_procedure/low_confidence, already detected upstream by
existing nodes) but not for the rest: "billing terms" and "explicit human
request" need no upstream tool result at all — just the customer's own
words — and the classifier-driven pair (frustration/complexity) needs no
upstream trigger EITHER, since DESIGN's combinator runs it whenever no hard
rule already fired, on every run, not only ones an earlier node happened to
flag. ``agent.nodes.decide`` (T-6 now owns ``backend/src/agent/**`` too)
therefore calls ``EscalationDecider.evaluate`` (above) — the full
combinator, hard rules then classifier — for every run that reaches it with
``state["route"] != "escalate"``. See that function's docstring for exactly
which routes that is (every non-escalate branch: case_status, permission,
kb, off_topic) and why calling it there, rather than duplicating the
combinator's hard-rule-then-classifier logic inline, is what guarantees a
fired hard rule always short-circuits the classifier.
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

    def evaluate(
        self,
        *,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision:
        """The full DESIGN combinator with NO assumed trigger — hard rules
        first, then (only if none fired) the classifier's escalate+
        confidence>=threshold verdict. ``agent.nodes.decide`` calls this,
        not ``decide`` above, for every run that reaches it without an
        earlier node already having routed to ``"escalate"`` — see that
        function's docstring for exactly which routes that is and why."""
        ...


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

    def evaluate(
        self,
        *,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision:
        """No trigger is handed to this stand-in path, and — per the class
        docstring — it does no judgment of its own, so it never escalates
        here. Kept only so this class still structurally satisfies
        ``EscalationDecider`` now that the Protocol requires the full
        combinator too; nothing in this repo constructs
        ``PlaceholderEscalationDecider`` as the live default, so this
        method's behavior is untested and irrelevant in practice."""
        return EscalationDecision(escalate=False, triggers=[])
