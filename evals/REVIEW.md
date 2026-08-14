# T-7 labeled-set review — please read this before approving anything

**Status: `evals/labeled_set.yaml` is `PROPOSED_AWAITING_HUMAN_REVIEW`. Nothing
downstream (a threshold, a reported precision/recall number, a "T-7 done")
happens until you act on this document.**

## What you're being asked to approve

`evals/labeled_set.yaml` holds ~50 fictional support tickets, each with a
label: what route the AI agent should land on (`case_status`, `permission`,
`kb`, `off_topic`, or `escalate`), and — for escalations — which of DESIGN's
seven reasons apply (`billing`, `human_request`, `unknown_case`,
`out_of_procedure`, `low_confidence`, `frustration`, `complexity`). These
labels are the **ground truth** `evals/report.py` measures the escalation
engine against: its confusion matrix, precision, recall, and any threshold
recommendation are only as trustworthy as this file's independence from the
system it's grading.

**I (the coding agent that implemented T-7) authored every label in this
file. I did not, and cannot, approve them — the acceptance criteria and
CLAUDE.md are both explicit that label approval is a human-only step, and
that the whole point is that this ground truth comes from someone other
than whoever built (or is grading) the system.** If I both wrote the system
and signed off on what "correct" means for it, a good score would prove
nothing. Your review is what makes the eventual number mean something.

**What to do when you're done:** open `evals/labeled_set.yaml`, and in its
`approval:` block, set `status: APPROVED`, fill in `approved_by` with your
name, and `approved_date`. Until then the report generator will keep
stamping every output `DRAFT — LABELS NOT YET APPROVED`, on purpose.

You do not have to accept my proposed label on anything. Where you disagree,
just change `expected_escalate` / `expected_reasons` / `expected_route`
directly in the YAML (or tell whoever's driving this to).

---

## Part 1 — Genuinely contestable (start here)

These five are the ones I think reasonable people could land differently
on. I've proposed an answer for each, but **the decision here is a product
judgment about your tolerance for over-escalating (a routine ticket costs a
human's attention it didn't need) versus under-escalating (a genuinely
unhappy customer gets a templated non-answer) — that tradeoff is yours to
make, not mine.** I've tried to state the case for the answer I didn't pick
as fairly as I can.

- [ ] **`esc-frustration-borderline-mild-01`** — *"It's been a while and I
  was hoping for an update, this is a bit frustrating honestly."*
  Proposed: **no escalate**.
  Case for escalating instead: the customer used the word "frustrating"
  unprompted — some teams treat any self-reported frustration as a signal
  worth a human touch, regardless of how mild the wording is, on the theory
  that customers under-report rather than over-report irritation in writing.

- [ ] **`esc-frustration-borderline-impatient-01`** — *"I know these things
  take time, but I'll admit I'm getting a little impatient waiting on my
  results."*
  Proposed: **no escalate**.
  Case for escalating instead: "I'll admit" reads as someone downplaying a
  real feeling to stay polite — arguably the customers most worth catching
  early are exactly the ones minimizing their own complaint.

- [ ] **`esc-frustration-borderline-near-window-01`** — *"Kind of annoyed I
  haven't heard back yet, though I do get that these things take time. Just
  wanted to say something."*
  Proposed: **no escalate**.
  Case for escalating instead: "just wanted to say something" is the
  customer explicitly flagging that this message itself is the ask — a
  templated status reply might read as not having listened to that at all.

- [ ] **`esc-complexity-borderline-two-part-01`** — *"Quick question — what
  stage is case MFG-2025-0865 in right now, and separately, could I also
  pay to rush whichever stage is next?"*
  Proposed: **no escalate** (answerable via case-status lookup + the rush
  policy explanation, both templated/KB-grounded).
  Case for escalating instead: it's two asks bundled into one message, and
  a system that only ever answers the first thing it sees in a multi-part
  message is a common, hard-to-detect failure mode — some teams would
  rather route anything bundled to a human than risk a half-answer.

- [ ] **`esc-complexity-borderline-stalled-01`** — *"It feels like my case
  has been sitting for a while without much movement. Is that normal, or is
  something wrong? Not upset, just want to understand what's going on and
  whether I should be doing anything differently."*
  Proposed: **no escalate** (answerable via the standard
  re-extraction/turnaround explanation).
  Case for escalating instead: "is something wrong" is an open-ended
  question the KB can only answer in general terms — it can't confirm
  *this specific case* isn't actually behind schedule, and
  `fixtures/kb/turnaround-times.md` itself says a case that seems stalled
  well beyond its window is a specialist-review matter; if you'd rather the
  agent err toward checking rather than reassuring, this should escalate.

---

## Part 2 — Clear-cut, grouped for fast skimming

I'm confident about these, but you should still skim them — a wrong label
here is a silent bug in the eval, not just a debatable one. Full text is in
`evals/labeled_set.yaml`; excerpts below.

### No escalation expected

- [ ] `cs-intake-01` — status check, intake stage — **case_status**
- [ ] `cs-extraction-01` — status check, stale-but-not-flagged extraction case — **case_status**
- [ ] `cs-sequencing-01` — status check, sequencing stage — **case_status**
- [ ] `cs-genealogy-01` — status + explanation, genealogy stage — **case_status**
- [ ] `cs-complete-01` — "is my case done" — **case_status**
- [ ] `perm-add-contact-01` — add spouse as authorized contact — **permission**
- [ ] `perm-add-contact-02` — add co-investigator as authorized contact — **permission**
- [ ] `perm-resend-report-01` — resend an already-delivered report — **permission**
- [ ] `perm-extend-retention-01` — extend retention 6 months (≤12mo) — **permission**
- [ ] `perm-extend-retention-02` — extend retention exactly 12 months (boundary) — **permission**
- [ ] `kb-turnaround-01` — general turnaround-time question — **kb**
- [ ] `kb-refund-eligibility-01` — refund eligibility, non-dispute — **kb**
- [ ] `kb-billing-structure-01` — how billing works, non-dispute — **kb**
- [ ] `kb-rush-eligibility-01` — rush pricing/eligibility question — **kb**
- [ ] `kb-retention-period-01` — general retention-period question — **kb**
- [ ] `kb-report-format-01` — report format question — **kb**
- [ ] `kb-genealogy-limits-01` — "is a match guaranteed" — **kb**
- [ ] `kb-reextraction-explain-01` — why extraction needed a retry — **kb**
- [ ] `kb-adversarial-invoice-mention-01` — *mentions* an invoice, asks a routine fee-structure question — **kb** — ADVERSARIAL: must NOT trip the billing hard rule
- [ ] `kb-adversarial-human-mention-01` — asks *whether* a human reviews samples (curiosity, not a request for one) — **kb** — ADVERSARIAL: must NOT trip the human-request hard rule
- [ ] `off-weather-01` / `off-unrelated-product-01` / `off-greeting-01` / `off-newsletter-01` / `off-retail-unrelated-01` — unrelated to the lab — **off_topic**

### Escalation expected — billing dispute (hard trigger)

- [ ] `esc-billing-double-charge-01` — charged twice for extraction — reasons: `billing`
- [ ] `esc-billing-wrong-amount-01` — polite tone, wrong amount charged — reasons: `billing` — ADVERSARIAL: politeness must not suppress a real dispute
- [ ] `esc-billing-refund-demand-01` — refund demand for a charge — reasons: `billing`

### Escalation expected — explicit human request (hard trigger)

- [ ] `esc-human_request-direct-01` — "talk to a real person" — reasons: `human_request`
- [ ] `esc-human_request-polite-01` — very polite phrasing — reasons: `human_request` — ADVERSARIAL: politeness must not suppress a real request
- [ ] `esc-human_request-bot-callout-01` — "I know this is a bot" — reasons: `human_request`
- [ ] `esc-combined-billing-human_request-01` — both at once — reasons: `billing`, `human_request`

### Escalation expected — unknown/unresolvable case (hard trigger)

- [ ] `esc-unknown_case-nonexistent-id-01` — references case `MFG-2025-9999` (not on file) — reasons: `unknown_case`
- [ ] `esc-unknown_case-nonexistent-id-02` — references case `MFG-2099-0001` (not on file) — reasons: `unknown_case`

### Escalation expected — out-of-procedure request (hard trigger)

- [ ] `esc-out_of_procedure-change-requester-01` — change the primary requester — reasons: `out_of_procedure`
- [ ] `esc-out_of_procedure-early-deletion-01` — early data-deletion request — reasons: `out_of_procedure`

### Escalation expected — low_confidence: empty KB retrieval (hard trigger)

- [ ] `esc-low_confidence-empty_retrieval-accreditation-01` — accreditation/customs question, not covered by any KB doc — reasons: `low_confidence`
- [ ] `esc-low_confidence-empty_retrieval-international-01` — international submission logistics, not covered by any KB doc — reasons: `low_confidence`

### Escalation expected — low_confidence: verifier/groundedness failure (hard trigger)

- [ ] `esc-low_confidence-verifier_failure-exact-date-01` — demands an exact calendar date the KB can't ground — reasons: `low_confidence`
- [ ] `esc-low_confidence-verifier_failure-summed-timeline-01` — demands a summed cross-stage total the KB explicitly says not to compute — reasons: `low_confidence`

### Escalation expected — classifier abstention (hard trigger)

- [ ] `esc-low_confidence-abstention-garbled-01` — garbled/unparseable message, standing in for a failed classifier call — reasons: `low_confidence`

### Escalation expected — frustration, clear-cut

- [ ] `esc-frustration-repeated-emails-01` — "fourth time I've emailed... extremely frustrated" — reasons: `frustration`
- [ ] `esc-frustration-furious-01` — "I am furious... completely ignored" — reasons: `frustration`
- [ ] `esc-frustration-repeated-asks-01` — "asked THREE times... unacceptable" — reasons: `frustration`

### Escalation expected — complexity, clear-cut

- [ ] `esc-complexity-entangled-rush-two-cases-01` — two cases, stacked rush questions, court-date timing — reasons: `complexity`
- [ ] `esc-complexity-entangled-failure-timeline-shipping-01` — re-extraction fees + timeline + international shipping, all at once — reasons: `complexity`

---

## What happens to the test suite once you approve

Flipping `evals/labeled_set.yaml`'s `approval:` block to `APPROVED` (with
your name and date) is expected to turn three tests in
`backend/tests/evals/` **red** — that is by design, not a regression. Each
one currently asserts "not approved yet"; once you've approved for real,
that assertion is now testing the wrong thing and needs to be updated to
assert the *approved* state instead. Do not treat a failure here as
something to silently patch — each one names exactly what changed and why.

1. **`backend/tests/evals/test_labeled_set.py::test_labels_are_not_self_approved`**
   (asserts `approval["status"] != "APPROVED"` and that
   `approved_by`/`approved_date` are empty) — once you've approved, replace
   its three assertions with the mirror image: `status == "APPROVED"` and
   `approved_by`/`approved_date` are non-empty and, ideally, assert
   `approved_by` is *not* the string used by the coding agent's commits
   (i.e. confirms a human name, not a re-run of automation).
2. **`backend/tests/evals/test_report.py::test_labeled_set_yaml_is_actually_not_approved_right_now`**
   (asserts `raw["approval"]["status"] != "APPROVED"`) — flip to
   `== "APPROVED"`; consider renaming to
   `..._is_actually_approved_right_now` so the name matches the assertion.
3. **`backend/tests/evals/test_report.py::test_report_refuses_a_final_report_while_labels_are_unapproved`**
   — once real, this test's entire premise (that the real fixture is
   unapproved) is gone, not just its exit-code line. Rewrite its body to
   match `test_report_would_render_differently_once_labels_are_approved`'s
   assertions but against the **real** fixture (no `--labeled-set`
   override): `result.returncode == 0`, `metrics["approved"] is True`,
   `"FINAL — "` in the report text, `"DRAFT"` not in it. Renaming to
   something like `test_report_produces_a_final_report_once_labels_are_approved`
   is recommended so the name stops describing refusal.

None of the other tests in this suite need to change at approval time —
they either never touch the real fixture's approval state (the synthetic-
fixture tests) or don't assert the real fixture's status at all.

---

## A note on independence

If anything in this file or in `evals/labeled_set.yaml` looks like it was
written to make the escalation engine's numbers look good, that's a bug in
my work, not a feature — please flag it. The whole reason this review step
exists is so the eventual precision/recall number is evidence *for* the
system, not evidence the system graded its own homework.
