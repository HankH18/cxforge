"""Escalation engine: hard rules, classifier, internal-note composition —
DESIGN §Escalation contract (T-6 boundary, consumed by T-7).

- ``escalation.schemas`` — the pinned ``Reason`` literal and
  ``EscalationCall`` classifier-output schema.
- ``escalation.config`` — ``CLASSIFIER_CONFIDENCE_THRESHOLD``, a
  provisional placeholder T-7 owns tuning against the labeled set.
- ``escalation.rules`` — every hard rule as its own pure, deterministic,
  individually-testable predicate (no LLM call anywhere in this module).
- ``escalation.classifier`` — the one ``LLMClient`` call that catches
  frustration/complexity, the fuzzy half of DESIGN's contract.
- ``escalation.notes`` — internal-note composition: a conversation summary,
  template-filled grounded facts, and the escalation reason enum, kept in
  clearly separated sections.
- ``escalation.engine`` — ``EscalationEngine``, the real
  ``agent.escalation_seam.EscalationDecider`` wired into
  ``agent.graph.run_agent`` by default, implementing DESIGN's pinned
  combinator: any hard rule OR (classifier escalate AND confidence >=
  threshold).

Each submodule is imported directly (``from escalation.rules import ...``),
mirroring ``agent``'s own submodule import style — nothing is re-exported
from this file.
"""
