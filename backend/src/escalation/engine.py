"""The real ``agent.escalation_seam.EscalationDecider`` — DESIGN §Escalation
contract's pinned combinator: "Final decision = any hard rule OR (classifier
escalate AND confidence >= threshold)."

Two entry points, deliberately different in what they assume:

- ``decide`` is the ``EscalationDecider`` Protocol method — the seam T-5
  left, called ONLY from ``agent.nodes`` sites that already hold a
  structurally-detected ``EscalationTrigger`` (an unresolvable case, an
  out-of-procedure permission request, an empty retrieval or failed
  verification, or — via ``detect_deterministic_hard_rule`` below — a
  billing dispute / explicit human request). A hard rule has therefore
  ALREADY fired by the time ``decide`` is called; DESIGN's combinator is an
  OR, so nothing the classifier could say changes an already-True decision.
  ``decide`` exploits exactly that: it short-circuits to ``escalate=True``
  without ever calling the classifier. This keeps every existing T-5 call
  site (and this repo's fake-``LLMClient``-backed graph/grounding test
  fixtures, none of which register a canned ``EscalationCall``) working
  unchanged — the classifier is never invoked on a path those fixtures
  don't anticipate.
- ``evaluate`` is the FULL combinator, run from scratch with no assumed
  trigger: hard rules first (including a fresh deterministic check), then
  — only if none fired — the classifier's own escalate+confidence>=threshold
  verdict (frustration, complexity, or abstention). This is DESIGN's
  contract exercised completely, and is what this ticket's own test suite
  (``backend/tests/escalation/**``) calls directly to prove the hard-rule
  predicates, the combinator's truth table, and the "hard rule beats the
  classifier" adversarial case.

Wiring note (see ``agent.nodes.decide`` and this ticket's final report):
under the CURRENT graph, only ``decide`` above is reachable from a live
run — ``evaluate``'s classifier-inclusive path (independently catching
frustration/complexity with NO hard rule already fired) has no call site in
the live graph, because adding one would require an LLMClient call on every
successful run, which breaks every graph/grounding test's fake LLM client
(it fails loudly on any schema it wasn't told to expect, by design). That
is a real, deliberate limitation, not an oversight — flagged rather than
silently resolved, exactly as instructed.
"""

from __future__ import annotations

from typing import Any

from agent.escalation_seam import EscalationDecision, EscalationTrigger
from agent.llm import LLMClient
from escalation import rules
from escalation.classifier import run_classifier
from escalation.config import CLASSIFIER_CONFIDENCE_THRESHOLD
from escalation.schemas import Reason
from helpdesk.models import Message, Ticket


def _latest_customer_message(conversation: list[Message]) -> str:
    for message in reversed(conversation):
        if message.author_kind == "customer":
            return message.text
    return ""


def detect_all_deterministic_hard_rules(message_text: str) -> list[EscalationTrigger]:
    """The two hard rules that need no upstream trigger and no tool result
    at all — DESIGN's "billing terms" and "explicit human request" — just
    the customer's own words. Pure, deterministic, and cheap enough to run
    on every message. Checked independently (a message can be both a
    billing dispute AND an explicit human request at once) so the internal
    note can list every reason that genuinely applies, not just the first
    one found."""
    triggers: list[EscalationTrigger] = []
    if rules.is_billing_dispute(message_text):
        triggers.append(
            EscalationTrigger(
                reason="billing",
                detail=(
                    f"Billing-dispute language detected in the customer's message: "
                    f"{message_text!r}"
                ),
            )
        )
    if rules.is_explicit_human_request(message_text):
        triggers.append(
            EscalationTrigger(
                reason="human_request",
                detail=(
                    "Explicit request for a human detected in the customer's "
                    f"message: {message_text!r}"
                ),
            )
        )
    return triggers


def detect_deterministic_hard_rule(message_text: str) -> EscalationTrigger | None:
    """Convenience singular form of ``detect_all_deterministic_hard_rules``
    for callers (``agent.nodes.decide``) that only need to know WHETHER a
    deterministic hard rule fired, to decide whether to hand a trigger to
    ``EscalationDecider.decide`` at all — which of the (possibly several)
    matched reasons is reported is irrelevant there, since
    ``EscalationEngine.decide`` independently re-derives the full set via
    ``detect_all_deterministic_hard_rules`` for the actual decision."""
    triggers = detect_all_deterministic_hard_rules(message_text)
    return triggers[0] if triggers else None


def _dedupe(triggers: list[EscalationTrigger]) -> list[EscalationTrigger]:
    """Stable de-duplication by ``reason`` — a run can independently trip
    the same reason twice (e.g. the upstream trigger already said
    "low_confidence" and nothing else adds information), and the internal
    note should list each reason once."""
    seen: set[str] = set()
    deduped: list[EscalationTrigger] = []
    for trigger in triggers:
        if trigger.reason in seen:
            continue
        seen.add(trigger.reason)
        deduped.append(trigger)
    return deduped


class EscalationEngine:
    """DESIGN §Escalation contract's real engine. Implements
    ``agent.escalation_seam.EscalationDecider`` structurally (a
    ``Protocol`` — no explicit subclassing needed)."""

    def __init__(
        self, *, llm: LLMClient, threshold: float = CLASSIFIER_CONFIDENCE_THRESHOLD
    ) -> None:
        self._llm = llm
        self._threshold = threshold

    def decide(
        self,
        *,
        trigger: EscalationTrigger,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision:
        """See module docstring: ``trigger`` alone already satisfies
        DESIGN's "any hard rule" disjunct, so this never calls the
        classifier — it only enriches ``triggers`` with a fresh
        deterministic (billing/human_request) re-check, in case the
        message independently trips one or both of those too."""
        triggers: list[EscalationTrigger] = [
            trigger,
            *detect_all_deterministic_hard_rules(_latest_customer_message(conversation)),
        ]
        return EscalationDecision(escalate=True, triggers=_dedupe(triggers))

    def evaluate(
        self,
        *,
        ticket: Ticket,
        conversation: list[Message],
        topic: str,
        tool_results: dict[str, Any],
    ) -> EscalationDecision:
        """The full combinator with no assumed trigger — DESIGN's pinned
        formula run in full: hard rules first (billing, human request,
        classifier abstention), then — only if none fired — the
        classifier's own escalate+confidence>=threshold verdict
        (frustration, complexity). See module docstring for why this is
        not wired into the live graph's default path."""
        message_text = _latest_customer_message(conversation)
        triggers: list[EscalationTrigger] = detect_all_deterministic_hard_rules(message_text)

        if triggers:
            # A hard rule already fired — DESIGN's OR means the classifier
            # cannot be outvoted by (or even needs to be consulted for) a
            # decision that is already True. See the adversarial test:
            # hard rules are not overridable by the model.
            return EscalationDecision(escalate=True, triggers=_dedupe(triggers))

        call = run_classifier(self._llm, conversation=conversation, topic=topic)

        if rules.is_classifier_abstention(call):
            triggers.append(
                EscalationTrigger(
                    reason="low_confidence",
                    detail="Escalation classifier abstained (no parseable verdict returned).",
                )
            )
            return EscalationDecision(escalate=True, triggers=_dedupe(triggers))

        assert call is not None  # narrowed by is_classifier_abstention above
        classifier_fired = call.escalate and call.confidence >= self._threshold
        if not classifier_fired:
            return EscalationDecision(escalate=False, triggers=[])

        reasons: list[Reason] = call.reasons or ["frustration"]
        for reason in reasons:
            triggers.append(
                EscalationTrigger(
                    reason=reason,
                    detail=(
                        f"Escalation classifier flagged {reason!r} (confidence="
                        f"{call.confidence:.2f} >= threshold={self._threshold:.2f})"
                    ),
                )
            )
        return EscalationDecision(escalate=True, triggers=_dedupe(triggers))
