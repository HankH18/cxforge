"""Classifier output — DESIGN §Escalation contract, reproduced verbatim:

    EscalationCall(escalate: bool, reasons: list[Reason], confidence: float)
    Reason = Literal["billing","human_request","unknown_case",
                      "out_of_procedure","low_confidence","frustration",
                      "complexity"]

``Reason`` is also the type ``agent.escalation_seam.EscalationTrigger.reason``
is widened to (from T-5's own restricted 3-value subset) — see that
module's docstring for why: T-5's structural detections only ever produce
``"unknown_case"``/``"out_of_procedure"``/``"low_confidence"``, but this
engine can emit any of the seven, and every ``EscalationTrigger`` this
package constructs (billing, human_request, and the classifier's own
frustration/complexity/low_confidence-via-abstention) needs the full set.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Reason = Literal[
    "billing",
    "human_request",
    "unknown_case",
    "out_of_procedure",
    "low_confidence",
    "frustration",
    "complexity",
]


class EscalationCall(BaseModel):
    """The escalation classifier's structured output — DESIGN's pinned
    shape, reproduced exactly. ``reasons`` is drawn from the full ``Reason``
    literal on the schema (so strict structured-output validation rejects
    anything else), even though in practice ``escalation.classifier``'s
    prompt only ever asks the model to report ``"frustration"``/
    ``"complexity"`` — the two DESIGN assigns to the classifier's own
    judgment; every other reason is a hard rule's job, never the model's."""

    escalate: bool
    reasons: list[Reason]
    confidence: float
