# Escalation methodology

> **R6** (`docs/SPEC.md`): escalate on any hard trigger (billing dispute,
> explicit human request, unknown/unresolvable case, out-of-procedure
> request, empty retrieval, low confidence, verifier failure) OR when the
> classifier flags frustration/complexity above threshold. Post an
> internal note, tag, assign to the escalation group, and tell the
> customer a specialist will follow up. Never guess.

This document covers the escalation methodology in code today, the
defect found and fixed in wiring the classifier into the live graph, and
the eval methodology and its current (draft) status.

## The hard rules — `backend/src/escalation/rules.py`

Seven conditions total, pinned by `docs/DESIGN.md`. Two are pure,
deterministic, no-LLM predicates over the customer's own words, checked
independently so a message can trip both at once:

- `is_billing_dispute` — scoped deliberately to genuine dispute/
  monetary-adjustment language ("charged twice", "billing dispute",
  "refund me"), not billing vocabulary in general — a bare "how does
  billing work?" question stays answerable via the `kb` route, per
  `fixtures/kb/billing-and-payment-terms.md`'s own stated line.
- `is_explicit_human_request` — "talk to a real person", "this is a bot",
  "escalate this to a human", drawn from
  `fixtures/kb/escalation-and-specialist-requests.md`'s own keyword list.

Three more are **detected upstream**, structurally, by the graph nodes
themselves before the escalation engine is ever consulted — not
re-derived by the engine, since that lookup is already done once by the
time `decide` runs:

- `is_unknown_case` — `agent/nodes.py:case_status`/`permission` couldn't
  resolve a case without guessing (missing, or on file for a different
  requester).
- `is_out_of_procedure` — `permission` matched no closed, KB-grounded
  always-grant kind.
- `is_low_confidence_trigger` — `kb_answer`'s retrieval came back empty,
  or `verify`'s groundedness score/grounding-guard check failed. Both
  collapse into the single `Reason` value `"low_confidence"` — DESIGN's
  pinned `Reason` literal has no separate value for the two.

The seventh, `is_classifier_abstention`, fires when
`escalation/classifier.py:run_classifier` returns `None` — a refusal,
truncation, or any exception the underlying `LLMClient` raises.
Escalating on `None` is itself deterministic control flow, even though
the condition it checks is the outcome of a model call.

## The classifier — `backend/src/escalation/classifier.py`

Exactly one `LLMClient.structured(EscalationCall, ...)` call, scoped by
its own system prompt to exactly the two signals DESIGN assigns the
model's judgment: **frustration** (angry, repeated themselves, feels
ignored) and **complexity** (tangled/entangled asks a templated or
KB-grounded answer would likely get wrong). The prompt explicitly tells
the model not to judge billing, human requests, or case/permission
resolvability — those are hard rules' job. Output schema
(`escalation/schemas.py`):

```python
class EscalationCall(BaseModel):
    escalate: bool
    reasons: list[Reason]
    confidence: float
```

## The combinator — `backend/src/escalation/engine.py`

`EscalationEngine.evaluate` is the full, pinned combinator: **hard rules
first** (billing, human request — via
`detect_all_deterministic_hard_rules` — plus classifier abstention); only
if none fired does it call the classifier, and only escalates on the
classifier's verdict if `call.escalate and call.confidence >=
self._threshold`. A fired hard rule always short-circuits before the
classifier is even consulted — `backend/tests/escalation/test_adversarial.py`
proves this at the unit level (a message reading as both an explicit
human request and something the classifier would wave through must still
escalate on the hard rule alone, with the classifier's contrary opinion
never even asked for), and `backend/tests/graph/test_live_escalation_classifier.py`
re-proves the same guarantee through the real compiled graph.

`EscalationEngine.decide` is a separate, narrower entry point used only
by graph nodes that already hold a structurally-detected
`EscalationTrigger` (an unresolvable case, an out-of-procedure request,
etc.) — since a hard rule has already fired by construction, it never
calls the classifier; it only enriches the trigger list with a fresh
billing/human-request re-check.

## The threshold is provisional

`escalation/config.py`:

```python
# PROVISIONAL DEFAULT — NOT TUNED.
CLASSIFIER_CONFIDENCE_THRESHOLD = 0.5
```

This value was picked only so the combinator and its own unit tests have
a concrete number to compare against. It has never been tuned against
`evals/labeled_set.yaml`, by explicit design — T-6's own non-goal was "no
threshold tuning (T-7 owns it)." T-7's report (below) computes a
*recommended* value of **0.59** against a stubbed classifier and
unapproved labels, and explicitly does not write it back here — see
"Current status" below for why.

## The defect: the classifier was dead code in the live graph

This is worth stating plainly because it was a real defect, not a
hypothetical one, and the fix is what `test_live_escalation_classifier.py`
exists to prove.

Before the fix, nothing in `agent/nodes.py` ever called
`escalation.classifier.run_classifier` unless a graph node had *already*
detected a structural hard-rule-equivalent condition (an unresolvable
case, an out-of-procedure request, empty retrieval, a failed
verification) — i.e., unless the decision to escalate had already been
made for an unrelated reason. Driving the real graph with a furious
customer message and no hard trigger routed the ticket straight to a KB
answer and closed it out; the escalation classifier was never even
called. The frustration/complexity half of R6 was unreachable from a live
run.

The fix: `agent/nodes.py:decide` now calls `EscalationDecider.evaluate`
(the full combinator) **unconditionally** for every run that reaches it
with `state["route"] != "escalate"` — that covers every branch
(`case_status`, `permission`, `kb`, `off_topic`), not only `kb`, because
DESIGN's frustration/complexity signal is about the customer's own
conversation, not about which route `classify` picked. A furious customer
asking a routine case-status question must still be escalatable.

`backend/tests/graph/test_live_escalation_classifier.py` proves this
against the real compiled graph with three cases: (1) a frustrated
customer, no hard trigger, classifier escalates above threshold → the run
now escalates (this is the confirmed-defect repro, replayed through the
fixed graph); (2) the mirror image, classifier says escalate but below
threshold → does not escalate; (3) the ordering guarantee at the
full-graph level (a fired hard rule always wins, the classifier is never
even consulted). `backend/tests/escalation/test_decide_node_wiring.py`
has a fourth test at the unit level,
`test_ordinary_message_no_hard_rule_consults_classifier_leaves_route_untouched`,
whose own docstring records that it **replaced** an earlier, incorrect
version of the same test that had asserted the opposite (`llm.calls ==
0` — i.e. that the classifier is *never* consulted for an ordinary
message) — that earlier assertion had encoded the exact defect this fix
closes.

## Internal-note composition — `backend/src/escalation/notes.py`

`compose_internal_note` renders three clearly headed sections so a human
reviewer can tell at a glance which is which:

- `=== CONVERSATION SUMMARY ===` — the one place free text is allowed;
  reuses `classify`'s own `topic` output rather than paying for a second
  LLM call to restate it.
- `=== GROUNDED FACTS ===` — template-filled from `tool_results` only
  (case fields, matched permission kind, retrieved KB doc slugs) — same
  R9 discipline as customer-facing templates, never free-generated.
- `=== ESCALATION REASON(S) ===` — the decision's own `EscalationTrigger`
  list, verbatim, never re-derived.

## Eval methodology

### The labeled set

`evals/labeled_set.yaml` holds 51 fictional support tickets, each labeled
with an expected route and (for escalations) which of DESIGN's seven
reasons apply. This is the ground truth `evals/report.py` measures the
engine against.

### Why human approval of labels is required

A system that supplies its own ground truth measures nothing. The
labeled set carries an `approval:` block whose comment is explicit: *"The
coding agent that authored these labels (T-7) MUST NOT set this to
APPROVED, MUST NOT fill in approved_by/approved_date, and MUST NOT infer
approval from anything short of the human explicitly saying so."*
`evals/REVIEW.md` is the accompanying document written for the human
reviewer — it flags five genuinely contestable borderline cases (mild
self-reported frustration, a bundled two-part question, a "feels stalled"
inquiry) with the case *against* the proposed label argued fairly, and
groups the other 46 for fast skimming. Its closing line states the point
directly: *"If anything in this file ... looks like it was written to
make the escalation engine's numbers look good, that's a bug in my work,
not a feature."*

### Current status: DRAFT, unapproved, classifier stubbed

`evals/labeled_set.yaml`'s `approval.status` is
**`PROPOSED_AWAITING_HUMAN_REVIEW`**, not `APPROVED`, as of this writing.
`docs/eval-report/report.md` (generated by `uv run python -m evals.report`)
is watermarked accordingly, at both the top and bottom of the file:

> **DRAFT — LABELS NOT YET APPROVED** ... Every number below is a DRAFT —
> proof the pipeline runs, not a real measurement.

Beyond the label-approval gap, the report itself documents that its
**classifier half is stubbed**: no `OPENAI_API_KEY` exists in this
environment, and the report assumes no live Postgres/pgvector connection
either, so only pure, deterministic checks run for real:

| Signal | Status |
|---|---|
| `is_billing_dispute`, `is_explicit_human_request` | **REAL** — run directly against ticket body text |
| `is_unknown_case` | **REAL** — checked against `fixtures/cases.yaml` directly, no live DB |
| `is_out_of_procedure`, both `low_confidence` subtypes, `is_classifier_abstention` | **STUBBED** — need a live permission matcher, KB search, groundedness judge, or classifier call; a replayable table stands in per ticket id |
| Frustration/complexity classifier verdict | **STUBBED** — a hand-authored, replayable verdict table, deliberately including a few entries wrong relative to the label so the confusion matrix demonstrates real disagreement-handling |

The report's own text is explicit: **"No number in this report should be
read as a measurement of the real OpenAI-backed classifier's accuracy."**

For what it's worth as a pipeline smoke test (not a real measurement),
the current draft numbers, from `docs/eval-report/metrics.json`:

- Confusion matrix: TP=21, FP=0, FN=0, TN=30 → precision/recall/F1 all
  1.000.
- Hard-trigger subset recall (16 tickets carrying a genuine hard trigger):
  1.000. The report's own methodology section notes this is expected to
  be 1.0 whenever the predictor correctly identifies the trigger, since a
  fired hard rule is threshold-independent by construction under the OR
  combinator — it reflects the combinator's design (real for
  billing/human_request/unknown_case, stubbed-but-always-firing for
  out_of_procedure/low_confidence here), not classifier accuracy.
- Recommended threshold from sweeping the *stubbed* classifier scores:
  0.59 (current provisional config value: 0.50). **Not written to
  `escalation/config.py`** — the report explicitly declines to, since a
  committed threshold requires human-approved labels first.

R15's actual requirement — "Recall on the hard-trigger subset ≥ 0.95",
measured against an approved labeled set — is **not yet met by any real
measurement**, because no approved measurement exists yet. What exists is
proof the report-generation pipeline itself works correctly end to end.

### What happens next

Once a human works through `evals/REVIEW.md` and sets
`evals/labeled_set.yaml`'s `approval.status` to `APPROVED` (with
`approved_by`/`approved_date` filled in), re-running `evals/report.py`
against real `OPENAI_API_KEY`/live Postgres would produce the first real
measurement. Until then, treat every number in
`docs/eval-report/report.md` as evidence the *harness* is correct, not
evidence about the *classifier*.

See `docs/architecture.md` for where the escalation engine sits in the
graph, and `docs/grounding.md` for the deterministic guard that forces
`low_confidence` escalations on the `kb` route independently of this
engine.
