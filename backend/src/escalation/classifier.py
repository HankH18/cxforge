"""The classifier half of the escalation engine — DESIGN §Escalation
contract: "Classifier via LLMClient emitting EscalationCall ... catches the
fuzzy cases hard rules cannot: frustration and complexity." Exactly one
``LLMClient.structured`` call, scoped to the pinned ``EscalationCall``
schema — never a second, separate sentiment model (ticket constraint: "No
sentiment model beyond the classifier prompt").
"""

from __future__ import annotations

import logging
from typing import Any

import openai

from agent.llm import LLMClient
from escalation.schemas import EscalationCall
from helpdesk.models import Message

logger = logging.getLogger(__name__)

# The prompt deliberately narrows the model's job to exactly the two
# reasons DESIGN assigns the classifier (frustration, complexity) — every
# other Reason value is a deterministic hard rule's job
# (``escalation.rules``), never a judgment call handed to the model.
ESCALATION_CLASSIFIER_SYSTEM = (
    "You triage support tickets for a forensic-genomics case lab's AI "
    "support agent, looking ONLY for two fuzzy signals that deterministic "
    "rules cannot catch:\n"
    "- frustration: the customer is angry, has repeated themselves, or "
    "clearly feels ignored or mistreated.\n"
    "- complexity: the question is tangled enough (multiple entangled "
    "asks, a genuinely unusual situation, or something well outside "
    "routine status/policy questions) that a templated or "
    "knowledge-base-grounded answer would likely be wrong or unhelpful.\n"
    "Do NOT judge billing disputes, requests for a human, or whether a "
    "case/permission could be resolved — those are handled separately by "
    "deterministic rules, not you. Set escalate=true only when frustration "
    "or complexity genuinely warrants a human specialist; when you do, "
    "list every reason that applies, using only \"frustration\" and/or "
    "\"complexity\" (never any other value), and give your confidence "
    "(0.0-1.0) in that judgment. If neither applies, set escalate=false, "
    "reasons=[], and give your confidence in THAT conclusion."
)


def _transcript(conversation: list[Message]) -> str:
    speaker_labels = {"customer": "Customer", "agent": "Agent", "ai": "AI"}
    return "\n".join(f"{speaker_labels[m.author_kind]}: {m.text}" for m in conversation)


def run_classifier(
    llm: LLMClient, *, conversation: list[Message], topic: str
) -> EscalationCall | None:
    """Run the escalation classifier, or return ``None`` on any *absorbable*
    failure to produce a usable verdict — an OpenAI SDK API/connection/
    timeout/refusal-shaped error, or the ``ValueError``
    ``OpenAILLMClient.structured`` itself raises on a refusal or truncated
    response (see ``agent/llm.py``). ``None`` is exactly DESIGN's
    "classifier abstention" hard-rule condition (see
    ``escalation.rules.is_classifier_abstention``); callers must treat it
    as a hard escalation trigger, never as "no opinion, proceed normally."

    Deliberately NOT caught here: anything else — a ``TypeError``,
    ``AttributeError``, ``KeyError``, an unregistered-schema
    ``AssertionError`` from a test double, or any other programming error.
    Those are bugs, not model-shaped failures; swallowing them into
    abstention would silently turn a defect in this path into a plausible-
    looking escalation instead of a loud crash (T-18). They propagate.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ESCALATION_CLASSIFIER_SYSTEM},
        {
            "role": "user",
            "content": f"Topic: {topic}\n\nConversation:\n{_transcript(conversation)}",
        },
    ]
    try:
        result = llm.structured(EscalationCall, messages)
    except (openai.OpenAIError, ValueError) as exc:
        logger.warning(
            "Escalation classifier abstained after %s: %s", type(exc).__name__, exc
        )
        return None
    if not isinstance(result, EscalationCall):
        logger.warning(
            "Escalation classifier abstained: structured() returned %s, not EscalationCall",
            type(result).__name__,
        )
        return None
    return result
