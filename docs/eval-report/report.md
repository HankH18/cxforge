# Escalation eval report — T-7 / T-21

**FINAL — labels approved (see evals/labeled_set.yaml's approval: block)**

Generated: 2026-08-15T23:01:02.513373+00:00
Labeled tickets: 51

## Methodology — what is REAL vs UNMEASURED

Every prediction below comes from calling `escalation.engine.EscalationEngine.evaluate`/`.decide` directly — this report has no escalation decision logic of its own (see `backend/tests/evals/test_no_divergence.py`).

- **REAL, no LLM call** — `billing` / `human_request`: checked by `EscalationEngine.evaluate()` itself, pure regex over the ticket body.
- **REAL, no LLM call** — `unknown_case`: a referenced `MFG-####-####` case id is checked for membership in `fixtures/cases.yaml` directly (independent of the label), then handed to `EscalationEngine.decide()` as a real trigger.
- **REAL, live Anthropic classifier** — `frustration` / `complexity` / classifier abstention: any ticket that reaches this tier is scored by a real `agent.llm.AnthropicLLMClient` call inside `EscalationEngine.evaluate()`. Whatever the model says — including a genuine abstention — is what gets reported.
- **UNMEASURED** — `out_of_procedure`, and `low_confidence`'s empty-retrieval / verifier-failure subtypes: these are detected by `agent.nodes` (a live pgvector KB search, plus a permission-match or groundedness-judge LLM call), not by `EscalationEngine` — there is no code path in the engine that reproduces this judgment, and reproducing `agent.nodes`' own pipelines here is out of this report's scope. See `evals/report.py`'s module docstring for the full rationale.

**Live classifier calls this run made: 36** (cached per ticket across the full threshold sweep — see module docstring).

## Label distribution

- Total tickets: 51
- Escalate: 21 / Not escalate: 30
- By route:
  - `case_status`: 10
  - `escalate`: 21
  - `kb`: 10
  - `off_topic`: 5
  - `permission`: 5
- By reason (a ticket can carry more than one):
  - `billing`: 4
  - `complexity`: 2
  - `frustration`: 3
  - `human_request`: 4
  - `low_confidence`: 5
  - `out_of_procedure`: 2
  - `unknown_case`: 2

## Unmeasured scenarios (excluded from every metric below)

6 of 51 tickets require `agent.nodes`-level infrastructure this report does not drive (see Methodology above) and are excluded from the confusion matrix, precision/recall/F1, and hard-trigger recall below — not defaulted to a guessed answer.

- `esc-out_of_procedure-change-requester-01` — out_of_procedure is detected by agent.nodes.permission (a live, KB-grounded permission-match LLM call over retrieved policy chunks) BEFORE EscalationEngine is ever consulted — EscalationEngine has no code path that re-derives this judgment.
- `esc-out_of_procedure-early-deletion-01` — out_of_procedure is detected by agent.nodes.permission (a live, KB-grounded permission-match LLM call over retrieved policy chunks) BEFORE EscalationEngine is ever consulted — EscalationEngine has no code path that re-derives this judgment.
- `esc-low_confidence-empty_retrieval-accreditation-01` — this low_confidence scenario is detected by agent.nodes.kb_answer (empty pgvector KB retrieval) or agent.nodes.verify (a live groundedness-judge LLM call over a composed draft) BEFORE EscalationEngine is ever consulted — same structural gap as out_of_procedure.
- `esc-low_confidence-empty_retrieval-international-01` — this low_confidence scenario is detected by agent.nodes.kb_answer (empty pgvector KB retrieval) or agent.nodes.verify (a live groundedness-judge LLM call over a composed draft) BEFORE EscalationEngine is ever consulted — same structural gap as out_of_procedure.
- `esc-low_confidence-verifier_failure-exact-date-01` — this low_confidence scenario is detected by agent.nodes.kb_answer (empty pgvector KB retrieval) or agent.nodes.verify (a live groundedness-judge LLM call over a composed draft) BEFORE EscalationEngine is ever consulted — same structural gap as out_of_procedure.
- `esc-low_confidence-verifier_failure-summed-timeline-01` — this low_confidence scenario is detected by agent.nodes.kb_answer (empty pgvector KB retrieval) or agent.nodes.verify (a live groundedness-judge LLM call over a composed draft) BEFORE EscalationEngine is ever consulted — same structural gap as out_of_procedure.

## Confusion matrix (binary escalate / not-escalate, 45 measured tickets, at recommended threshold)

| | Predicted escalate | Predicted no-escalate |
|---|---|---|
| **Actual escalate** | TP=15 | FN=0 |
| **Actual no-escalate** | FP=0 | TN=30 |

## Precision / Recall / F1 (at recommended threshold, measured tickets only)

- Precision: 1.000
- Recall: 1.000
- F1: 1.000

## Hard-trigger subset recall (measured tickets only)

Recall on the 10 MEASURED tickets labeled with a genuine DESIGN hard trigger (billing/human_request/unknown_case/out_of_procedure/low_confidence, excluding frustration/complexity-only escalations): **1.000**, measured against the real engine (T-7 acceptance 4 / T-21 acceptance 5).
This is the real number, whatever it is — T-21's own non-goal is explicit that a miss here is a finding to report, not a threshold/subset to adjust. It does NOT include the out_of_procedure / low_confidence-structural tickets listed above as unmeasured; see that section for why, and treat this recall figure as coverage of a real subset, not the full DESIGN hard-trigger set.

## Recommended threshold

Sweeping `CLASSIFIER_CONFIDENCE_THRESHOLD` over the real classifier's scores (measured tickets only) and maximizing F1 recommends **0.00** (current provisional value in `backend/src/escalation/config.py`: 0.50).
**This value is NOT written to `backend/src/escalation/config.py`.** T-21's scope is `evals/report.py` + tests + `docs/eval-report/` only — this report computes and states a recommendation, machine-checkable in `metrics.json`, never commits it.

## PR curve

![PR curve](pr_curve.png)

## Per-ticket predictions

| id | expected | measured | predicted | engine triggers |
|---|---|---|---|---|
| `cs-intake-01` | no-escalate | yes | no-escalate | — |
| `cs-extraction-01` | no-escalate | yes | no-escalate | — |
| `cs-sequencing-01` | no-escalate | yes | no-escalate | — |
| `cs-genealogy-01` | no-escalate | yes | no-escalate | — |
| `cs-complete-01` | no-escalate | yes | no-escalate | — |
| `perm-add-contact-01` | no-escalate | yes | no-escalate | — |
| `perm-add-contact-02` | no-escalate | yes | no-escalate | — |
| `perm-resend-report-01` | no-escalate | yes | no-escalate | — |
| `perm-extend-retention-01` | no-escalate | yes | no-escalate | — |
| `perm-extend-retention-02` | no-escalate | yes | no-escalate | — |
| `kb-turnaround-01` | no-escalate | yes | no-escalate | — |
| `kb-refund-eligibility-01` | no-escalate | yes | no-escalate | — |
| `kb-billing-structure-01` | no-escalate | yes | no-escalate | — |
| `kb-rush-eligibility-01` | no-escalate | yes | no-escalate | — |
| `kb-retention-period-01` | no-escalate | yes | no-escalate | — |
| `kb-report-format-01` | no-escalate | yes | no-escalate | — |
| `kb-genealogy-limits-01` | no-escalate | yes | no-escalate | — |
| `kb-reextraction-explain-01` | no-escalate | yes | no-escalate | — |
| `kb-adversarial-invoice-mention-01` | no-escalate | yes | no-escalate | — |
| `kb-adversarial-human-mention-01` | no-escalate | yes | no-escalate | — |
| `off-weather-01` | no-escalate | yes | no-escalate | — |
| `off-unrelated-product-01` | no-escalate | yes | no-escalate | — |
| `off-greeting-01` | no-escalate | yes | no-escalate | — |
| `off-newsletter-01` | no-escalate | yes | no-escalate | — |
| `off-retail-unrelated-01` | no-escalate | yes | no-escalate | — |
| `esc-billing-double-charge-01` | escalate | yes | escalate | Billing-dispute language detected in the customer's message: 'I was charged twice for my extraction fee this month — this is a billing error and I want it fixed.\n' |
| `esc-billing-wrong-amount-01` | escalate | yes | escalate | Billing-dispute language detected in the customer's message: "Hi! I hope you're doing well. I think I may have been charged the wrong amount for my extraction fee — could someone please look into it? Thank you so much!\n" |
| `esc-billing-refund-demand-01` | escalate | yes | escalate | Billing-dispute language detected in the customer's message: "Please issue a refund for the sequencing fee, I don't think it should have been charged in the first place.\n" |
| `esc-human_request-direct-01` | escalate | yes | escalate | Explicit request for a human detected in the customer's message: 'I want to talk to a real person, not a bot.\n' |
| `esc-human_request-polite-01` | escalate | yes | escalate | Explicit request for a human detected in the customer's message: 'Would it be possible to speak with a human representative about this? No rush at all, thank you!\n' |
| `esc-human_request-bot-callout-01` | escalate | yes | escalate | Explicit request for a human detected in the customer's message: 'I know this is a bot. I need an actual person to help me, please.\n' |
| `esc-combined-billing-human_request-01` | escalate | yes | escalate | Billing-dispute language detected in the customer's message: 'I need to talk to a real person about this billing error, I was charged twice!\n'; Explicit request for a human detected in the customer's message: 'I need to talk to a real person about this billing error, I was charged twice!\n' |
| `esc-unknown_case-nonexistent-id-01` | escalate | yes | escalate | [REAL, independent of the label] referenced case id 'MFG-2025-9999' is absent from fixtures/cases.yaml |
| `esc-unknown_case-nonexistent-id-02` | escalate | yes | escalate | [REAL, independent of the label] referenced case id 'MFG-2099-0001' is absent from fixtures/cases.yaml |
| `esc-out_of_procedure-change-requester-01` | escalate | no | UNMEASURED | — |
| `esc-out_of_procedure-early-deletion-01` | escalate | no | UNMEASURED | — |
| `esc-low_confidence-empty_retrieval-accreditation-01` | escalate | no | UNMEASURED | — |
| `esc-low_confidence-empty_retrieval-international-01` | escalate | no | UNMEASURED | — |
| `esc-low_confidence-verifier_failure-exact-date-01` | escalate | no | UNMEASURED | — |
| `esc-low_confidence-verifier_failure-summed-timeline-01` | escalate | no | UNMEASURED | — |
| `esc-low_confidence-abstention-garbled-01` | escalate | yes | escalate | Escalation classifier flagged 'frustration' (confidence=0.78 >= threshold=0.00) |
| `esc-frustration-repeated-emails-01` | escalate | yes | escalate | Escalation classifier flagged 'frustration' (confidence=0.97 >= threshold=0.00) |
| `esc-frustration-furious-01` | escalate | yes | escalate | Escalation classifier flagged 'frustration' (confidence=0.95 >= threshold=0.00) |
| `esc-frustration-repeated-asks-01` | escalate | yes | escalate | Escalation classifier flagged 'frustration' (confidence=0.95 >= threshold=0.00) |
| `esc-frustration-borderline-mild-01` | no-escalate | yes | no-escalate | — |
| `esc-frustration-borderline-impatient-01` | no-escalate | yes | no-escalate | — |
| `esc-frustration-borderline-near-window-01` | no-escalate | yes | no-escalate | — |
| `esc-complexity-entangled-rush-two-cases-01` | escalate | yes | escalate | Escalation classifier flagged 'complexity' (confidence=0.82 >= threshold=0.00) |
| `esc-complexity-entangled-failure-timeline-shipping-01` | escalate | yes | escalate | Escalation classifier flagged 'complexity' (confidence=0.82 >= threshold=0.00) |
| `esc-complexity-borderline-two-part-01` | no-escalate | yes | no-escalate | — |
| `esc-complexity-borderline-stalled-01` | no-escalate | yes | no-escalate | — |

---
**FINAL — labels approved (see evals/labeled_set.yaml's approval: block)**
