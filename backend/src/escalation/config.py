"""Provisional config for T-6's classifier confidence threshold.

DESIGN §Escalation contract, pinned combinator: "Final decision = any hard
rule OR (classifier escalate AND confidence >= threshold). Threshold chosen
on the labeled set (T-7) and stored in config." T-6's own non-goal (per
docs/tickets.json): "No threshold tuning (T-7 owns it)."

``CLASSIFIER_CONFIDENCE_THRESHOLD`` below is therefore a PROVISIONAL
placeholder only, picked so ``escalation.engine.EscalationEngine`` and its
tests have a concrete number to combine against — it is never measured
against ``evals/labeled_set.yaml``. T-7 replaces this single constant with
the value chosen empirically to hit R15's recall >= 0.95 target on the
hard-trigger subset, and commits that value here (this exact name, this
exact module) once its report lands in docs/eval-report/. Do not tune this
value in T-6, or any ticket other than T-7.
"""

from __future__ import annotations

# PROVISIONAL DEFAULT — NOT TUNED. See module docstring: T-7 owns choosing
# this against the labeled set. 0.5 is an arbitrary midpoint, picked only so
# the combinator and its tests are well-defined, not a measured value.
CLASSIFIER_CONFIDENCE_THRESHOLD = 0.5
