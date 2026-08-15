"""T-7 escalation eval report generator — DESIGN §Verification strategy /
SPEC R15: confusion matrix, precision/recall/F1, and a PR curve image with
the chosen threshold marked, computed against ``evals/labeled_set.yaml``.

Run as::

    uv run python -m evals.report

from the repo root. Writes ``docs/eval-report/report.md``,
``docs/eval-report/pr_curve.png``, and ``docs/eval-report/metrics.json``.

WHAT IS REAL VS STUBBED (read this before trusting a number below)
--------------------------------------------------------------------
This environment has no ``OPENAI_API_KEY`` (T-7's own instructions) and no
guarantee of a live Postgres/pgvector connection for this script's purposes,
so only the parts of ``backend/src/escalation/rules.py`` that are pure,
deterministic Python over the ticket's own text (or a static repo fixture)
are exercised for real:

- ``rules.is_billing_dispute`` / ``rules.is_explicit_human_request`` — REAL.
  Run directly against every ticket's ``body``.
- ``rules.is_unknown_case`` — REAL, via a lightweight, static, DB-free
  stand-in for T-5's case resolution: a referenced ``MFG-####-####`` case id
  is looked up against ``fixtures/cases.yaml`` directly (no Postgres). This
  is a SIMPLIFIED version of ``agent.nodes._resolve_case`` (it does not also
  check the requester's own email against the case), but it is genuinely
  independent of this file's own labels, not a replay of them.
- ``rules.is_out_of_procedure``, the two ``low_confidence`` subtypes (empty
  KB retrieval, verifier-score failure), and ``rules.is_classifier_abstention``
  — STUBBED. These require a live permission-matching LLM call, a live
  pgvector KB search, a live groundedness judge, or a live escalation
  classifier call respectively — none of which this static, portable report
  attempts to run. A small REPLAYABLE table below (``STUB_STRUCTURAL_REASON``,
  ``STUB_ABSTENTION_IDS``) stands in for what the live graph would have
  computed, keyed by the specific ticket ids authored to represent each
  scenario (see ``evals/labeled_set.yaml``'s id-naming convention). This
  exercises the real ``rules.py`` predicate functions end-to-end, but is NOT
  an independent measurement for those categories.
- The escalation CLASSIFIER's own frustration/complexity verdict — STUBBED.
  ``STUB_CLASSIFIER_VERDICTS`` below is a small, hand-authored, replayable
  table of canned ``(escalate, reasons, confidence)`` verdicts, standing in
  for ``escalation.classifier.run_classifier``. A few entries are
  DELIBERATELY WRONG relative to the ticket's label (see the comments next
  to the table) so the confusion matrix and PR curve this script produces
  demonstrate real disagreement-handling, rather than a suspicious,
  uninformative all-correct diagonal that would prove nothing about the
  harness.

Every number in the generated report is prefixed with which of the above it
depends on. Nothing here should be read as a measurement of the real
OpenAI-backed classifier's accuracy — that measurement can only happen once
an OPENAI_API_KEY exists and the labels are human-approved (see the DRAFT
watermark logic below and evals/REVIEW.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

# `uv run python -m evals.report` adds the repo root (cwd) to sys.path, but
# NOT backend/src — that only happens automatically under pytest, via
# [tool.pytest.ini_options].pythonpath in pyproject.toml. This script is run
# directly, not under pytest, so it sets up its own import path first. Every
# import that depends on this MUST come after it (hence the noqa: E402s
# below) — see scripts/live_smoke.py for the same repo's precedent of a
# deferred/guarded import for a script run outside pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from escalation import rules  # noqa: E402
from escalation.config import CLASSIFIER_CONFIDENCE_THRESHOLD  # noqa: E402

LABELED_SET_PATH = _REPO_ROOT / "evals" / "labeled_set.yaml"
CASES_PATH = _REPO_ROOT / "fixtures" / "cases.yaml"
DOCS_DIR = _REPO_ROOT / "docs"
OUTPUT_DIR = DOCS_DIR / "eval-report"

CASE_ID_RE = re.compile(r"MFG-\d{4}-\d{4}")

HARD_TRIGGER_REASONS = {
    "billing",
    "human_request",
    "unknown_case",
    "out_of_procedure",
    "low_confidence",
}
CLASSIFIER_REASONS = {"frustration", "complexity"}

DRAFT_WATERMARK = "DRAFT — LABELS NOT YET APPROVED"

# ---------------------------------------------------------------------------
# STUB tables — see the module docstring's "WHAT IS REAL VS STUBBED" section.
# Keyed by the exact ticket ids authored in evals/labeled_set.yaml for each
# scenario. Kept intentionally small: every id here is deliberate, not a
# blanket default.
# ---------------------------------------------------------------------------

STUB_STRUCTURAL_REASON: dict[str, str] = {
    # out_of_procedure — requires a live permission-matching LLM call
    # against the KB's always-grant list (agent.nodes.permission).
    "esc-out_of_procedure-change-requester-01": "out_of_procedure",
    "esc-out_of_procedure-early-deletion-01": "out_of_procedure",
    # low_confidence via empty KB retrieval — requires a live pgvector
    # search (agent.nodes.kb_answer).
    "esc-low_confidence-empty_retrieval-accreditation-01": "low_confidence",
    "esc-low_confidence-empty_retrieval-international-01": "low_confidence",
    # low_confidence via verifier failure — requires a live groundedness
    # judge over a composed draft (agent.nodes.verify).
    "esc-low_confidence-verifier_failure-exact-date-01": "low_confidence",
    "esc-low_confidence-verifier_failure-summed-timeline-01": "low_confidence",
}

# classifier abstention — requires a real LLM call that fails/refuses to
# parse (escalation.classifier.run_classifier returning None).
STUB_ABSTENTION_IDS: set[str] = {"esc-low_confidence-abstention-garbled-01"}

# The escalation classifier's own (escalate, reasons, confidence) verdict —
# entirely fabricated (no OPENAI_API_KEY in this environment). Confidence
# is on DESIGN's own 0.0-1.0 scale, and (per escalation/classifier.py's
# prompt) is confidence IN WHATEVER CONCLUSION escalate reflects, not
# P(escalate) — matching escalation.engine.EscalationEngine.evaluate's own
# `call.escalate and call.confidence >= threshold` semantics exactly.
StubVerdict = tuple[bool, list[str], float]
STUB_CLASSIFIER_VERDICTS: dict[str, StubVerdict] = {
    "esc-frustration-repeated-emails-01": (True, ["frustration"], 0.88),
    "esc-frustration-furious-01": (True, ["frustration"], 0.93),
    "esc-frustration-repeated-asks-01": (True, ["frustration"], 0.81),
    # DELIBERATELY WRONG (label: expected_escalate=false) — a plausible
    # classifier over-read of mild wording, to exercise a real false
    # positive in the confusion matrix rather than an all-correct diagonal.
    "esc-frustration-borderline-mild-01": (True, ["frustration"], 0.55),
    "esc-frustration-borderline-impatient-01": (False, [], 0.70),
    "esc-frustration-borderline-near-window-01": (True, ["frustration"], 0.45),
    "esc-complexity-entangled-rush-two-cases-01": (True, ["complexity"], 0.75),
    "esc-complexity-entangled-failure-timeline-shipping-01": (True, ["complexity"], 0.70),
    "esc-complexity-borderline-two-part-01": (False, [], 0.65),
    # DELIBERATELY WRONG (label: expected_escalate=false) — see above.
    "esc-complexity-borderline-stalled-01": (True, ["complexity"], 0.58),
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_labeled_set(path: Path = LABELED_SET_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text())
    header = {"approval": raw.get("approval", {}), "meta": raw.get("meta", {})}
    tickets: list[dict[str, Any]] = raw["tickets"]
    return header, tickets


def load_known_case_ids(path: Path = CASES_PATH) -> set[str]:
    raw = yaml.safe_load(path.read_text())
    return {c["case_id"] for c in raw["cases"]}


def is_approved(header: dict[str, Any]) -> bool:
    """The refuse-vs-final gate. True only if a human has EXPLICITLY flipped
    ``approval.status`` to ``APPROVED`` and recorded who and when — anything
    else (missing header, ``PROPOSED_AWAITING_HUMAN_REVIEW``, an
    ``APPROVED`` status with no name/date attached) is treated as NOT
    approved. This is the one function that decides whether the rest of
    this module renders a DRAFT-watermarked report or a final one — see
    ``report_status_banner``/``render_report``."""
    approval = header.get("approval") or {}
    return (
        approval.get("status") == "APPROVED"
        and bool(approval.get("approved_by"))
        and bool(approval.get("approved_date"))
    )


def canonical_approval_header() -> dict[str, Any]:
    """The approval gate's ONLY input (T-25 acceptance 1).

    ``--labeled-set`` substitutes the set that is *scored and rendered*; it
    must never be able to answer the question "has a human approved these
    labels?". That question is settled exclusively by the committed
    ``evals/labeled_set.yaml`` at ``LABELED_SET_PATH``, so pointing the CLI
    at a doctored copy with ``approval.status: APPROVED`` yields a DRAFT
    render and a non-zero exit like any other unapproved run. To exercise
    the approved direction of the gate, a test builds a synthetic repo tree
    whose own ``evals/labeled_set.yaml`` is the approved fixture — there is
    no flag, and no environment variable, that redirects this read."""
    header, _ = load_labeled_set(LABELED_SET_PATH)
    return header


def writes_into_docs(output_dir: Path) -> bool:
    """True when ``output_dir`` lands anywhere under the repo's ``docs/``.

    ``docs/`` is the published, human-facing tree; T-25 acceptance 2 keeps
    unapproved numbers out of it entirely rather than relying on a DRAFT
    watermark inside a file that lives among the real deliverables."""
    try:
        output_dir.resolve().relative_to(DOCS_DIR.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Prediction — mirrors escalation.engine.EscalationEngine.evaluate's own
# precedence exactly: Tier 1 (structural triggers T-5's graph nodes detect
# BEFORE decide()/the classifier is ever reached — see
# agent.escalation_seam's module docstring), Tier 2 (evaluate()'s own hard
# rules: text rules, then classifier abstention), Tier 3 (the classifier's
# thresholded frustration/complexity verdict).
# ---------------------------------------------------------------------------


@dataclass
class Prediction:
    escalate: bool
    reasons: list[str]
    score: float  # threshold-independent "risk score" — see ticket_score()
    real_signals: list[str] = field(default_factory=list)
    stubbed_signals: list[str] = field(default_factory=list)


def _find_case_id(text: str) -> str | None:
    match = CASE_ID_RE.search(text)
    return match.group(0) if match else None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _tier12_and_abstention(
    ticket: dict[str, Any], known_case_ids: set[str]
) -> tuple[bool, list[str], list[str], list[str]]:
    body: str = ticket["body"]
    ticket_id: str = ticket["id"]
    real_signals: list[str] = []
    stubbed_signals: list[str] = []
    reasons: list[str] = []

    # -- Tier 1: structural triggers (unknown_case/out_of_procedure/
    # low_confidence) — agent.nodes' case_status/permission/kb_answer/
    # verify detect these BEFORE decide() is reached, and
    # EscalationDecider.decide never consults the classifier once one has
    # fired (escalation.engine module docstring).
    referenced_case_id = _find_case_id(body)
    structural_reason: str | None = None
    if referenced_case_id is not None and referenced_case_id not in known_case_ids:
        structural_reason = "unknown_case"
        real_signals.append(
            f"unknown_case [REAL: {referenced_case_id!r} absent from fixtures/cases.yaml]"
        )
    elif ticket_id in STUB_STRUCTURAL_REASON:
        structural_reason = STUB_STRUCTURAL_REASON[ticket_id]
        stubbed_signals.append(
            f"{structural_reason} [STUBBED: replayed label — needs a live permission "
            "matcher / KB search / groundedness judge this report does not run]"
        )

    tier1_fired = False
    if rules.is_unknown_case(structural_reason):
        reasons.append("unknown_case")
        tier1_fired = True
    if rules.is_out_of_procedure(structural_reason):
        reasons.append("out_of_procedure")
        tier1_fired = True
    if rules.is_low_confidence_trigger(structural_reason):
        reasons.append("low_confidence")
        tier1_fired = True

    if tier1_fired:
        # EscalationEngine.decide re-runs the pure-text hard rules alongside
        # an already-fired structural trigger — mirrored here.
        if rules.is_billing_dispute(body):
            reasons.append("billing")
            real_signals.append("billing [REAL: rules.is_billing_dispute]")
        if rules.is_explicit_human_request(body):
            reasons.append("human_request")
            real_signals.append("human_request [REAL: rules.is_explicit_human_request]")
        return True, _dedupe(reasons), real_signals, stubbed_signals

    # -- Tier 2: evaluate()'s own hard rules — text rules (real), then
    # classifier abstention (stubbed, only for the id authored to represent
    # it).
    if rules.is_billing_dispute(body):
        reasons.append("billing")
        real_signals.append("billing [REAL: rules.is_billing_dispute]")
    if rules.is_explicit_human_request(body):
        reasons.append("human_request")
        real_signals.append("human_request [REAL: rules.is_explicit_human_request]")
    if reasons:
        return True, _dedupe(reasons), real_signals, stubbed_signals

    if ticket_id in STUB_ABSTENTION_IDS:
        simulated_classifier_call = None  # stand-in for run_classifier() -> None
        if rules.is_classifier_abstention(simulated_classifier_call):
            stubbed_signals.append(
                "classifier abstention [STUBBED: no OPENAI_API_KEY in this "
                "environment; replayed for this id]"
            )
            return True, ["low_confidence"], real_signals, stubbed_signals

    return False, [], real_signals, stubbed_signals


def _tier3_classifier_stub(ticket: dict[str, Any]) -> StubVerdict:
    return STUB_CLASSIFIER_VERDICTS.get(ticket["id"], (False, [], 0.0))


def ticket_score(ticket: dict[str, Any], known_case_ids: set[str]) -> float:
    """A threshold-independent "risk score" in [0, 1]: 1.0 for any ticket a
    fired hard rule (real or stubbed) or a stubbed classifier abstention
    already decides — DESIGN's OR combinator means these never depend on
    the threshold — otherwise the stubbed classifier's own confidence (only
    when its stubbed verdict is escalate=True; a stubbed escalate=False
    verdict never contributes to a positive prediction at any threshold,
    matching ``EscalationEngine.evaluate``'s own
    ``call.escalate and call.confidence >= threshold`` short-circuit)."""
    fired, _reasons, _real, _stub = _tier12_and_abstention(ticket, known_case_ids)
    if fired:
        return 1.0
    stub_escalate, _stub_reasons, confidence = _tier3_classifier_stub(ticket)
    return confidence if stub_escalate else 0.0


def predict_ticket(
    ticket: dict[str, Any], *, threshold: float, known_case_ids: set[str]
) -> Prediction:
    fired, reasons, real_signals, stubbed_signals = _tier12_and_abstention(ticket, known_case_ids)
    if fired:
        return Prediction(
            escalate=True,
            reasons=reasons,
            score=1.0,
            real_signals=real_signals,
            stubbed_signals=stubbed_signals,
        )

    stub_escalate, stub_reasons, confidence = _tier3_classifier_stub(ticket)
    stubbed_signals = [
        *stubbed_signals,
        f"classifier verdict [STUBBED]: escalate={stub_escalate}, confidence={confidence:.2f}",
    ]
    escalate = stub_escalate and confidence >= threshold
    return Prediction(
        escalate=escalate,
        reasons=list(stub_reasons) if escalate else [],
        score=(confidence if stub_escalate else 0.0),
        real_signals=real_signals,
        stubbed_signals=stubbed_signals,
    )


def run_predictions(
    tickets: list[dict[str, Any]], *, threshold: float, known_case_ids: set[str]
) -> dict[str, Prediction]:
    return {
        t["id"]: predict_ticket(t, threshold=threshold, known_case_ids=known_case_ids)
        for t in tickets
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def confusion_matrix(
    tickets: list[dict[str, Any]], predictions: dict[str, Prediction]
) -> dict[str, int]:
    tp = fp = fn = tn = 0
    for t in tickets:
        expected = bool(t["expected_escalate"])
        predicted = predictions[t["id"]].escalate
        if expected and predicted:
            tp += 1
        elif not expected and predicted:
            fp += 1
        elif expected and not predicted:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def precision_recall_f1(cm: dict[str, int]) -> tuple[float, float, float]:
    tp, fp, fn = cm["tp"], cm["fp"], cm["fn"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def hard_trigger_recall(
    tickets: list[dict[str, Any]], predictions: dict[str, Prediction]
) -> tuple[float | None, int]:
    """Recall restricted to tickets whose label is a genuine DESIGN hard
    trigger (billing/human_request/unknown_case/out_of_procedure/
    low_confidence) — excludes frustration/complexity-only escalations,
    which are classifier judgment, not a hard trigger. Under DESIGN's OR
    combinator, a fired hard rule is threshold-independent by construction
    (real for billing/human_request/unknown_case; stubbed-but-always-firing
    for out_of_procedure/low_confidence/abstention here) — so this number
    is expected to be 1.0 whenever ``predict_ticket`` correctly identifies
    the trigger, not a measurement of classifier accuracy."""
    subset = [
        t
        for t in tickets
        if t["expected_escalate"] and set(t["expected_reasons"]) & HARD_TRIGGER_REASONS
    ]
    if not subset:
        return None, 0
    hits = sum(1 for t in subset if predictions[t["id"]].escalate)
    return hits / len(subset), len(subset)


def label_distribution(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    routes = Counter(t["expected_route"] for t in tickets)
    reasons = Counter(r for t in tickets for r in t["expected_reasons"])
    escalate = sum(1 for t in tickets if t["expected_escalate"])
    return {
        "total": len(tickets),
        "routes": dict(sorted(routes.items())),
        "escalate": escalate,
        "not_escalate": len(tickets) - escalate,
        "reasons": dict(sorted(reasons.items())),
    }


def sweep_thresholds(
    tickets: list[dict[str, Any]], known_case_ids: set[str], *, steps: int = 101
) -> list[dict[str, Any]]:
    results = []
    for threshold in np.linspace(0.0, 1.0, steps):
        threshold = float(threshold)
        preds = run_predictions(tickets, threshold=threshold, known_case_ids=known_case_ids)
        cm = confusion_matrix(tickets, preds)
        precision, recall, f1 = precision_recall_f1(cm)
        results.append(
            {"threshold": threshold, "precision": precision, "recall": recall, "f1": f1, "cm": cm}
        )
    return results


def recommend_threshold(sweep: list[dict[str, Any]]) -> float:
    """The threshold maximizing F1 across the sweep; ties broken toward the
    SMALLEST threshold (prefer catching more classifier-judged cases when
    several thresholds tie on F1). This is a RECOMMENDATION only — T-7's
    instructions are explicit that ``CLASSIFIER_CONFIDENCE_THRESHOLD`` in
    backend/src/escalation/config.py stays untouched until labels are
    human-approved; see the report's own "Recommended threshold" section."""
    best = max(sweep, key=lambda r: (r["f1"], -r["threshold"]))
    return best["threshold"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def report_status_banner(approved: bool) -> str:
    if approved:
        return "FINAL — labels approved (see evals/labeled_set.yaml's approval: block)"
    return f"{DRAFT_WATERMARK} (see evals/labeled_set.yaml's approval: block and evals/REVIEW.md)"


def plot_pr_curve(
    sweep: list[dict[str, Any]], recommended_threshold: float, approved: bool, out_path: Path
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    recalls = [r["recall"] for r in sweep]
    precisions = [r["precision"] for r in sweep]
    recommended = min(sweep, key=lambda r: abs(r["threshold"] - recommended_threshold))

    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot(
        recalls,
        precisions,
        "-",
        color="#2b6cb0",
        linewidth=2,
        label="Precision/Recall (threshold sweep)",
    )
    ax.scatter(
        [recommended["recall"]],
        [recommended["precision"]],
        color="crimson",
        zorder=5,
        s=70,
        label=(
            f"recommended threshold={recommended['threshold']:.2f} "
            f"(P={recommended['precision']:.2f}, R={recommended['recall']:.2f})"
        ),
    )
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    title = "Escalation decision — Precision/Recall curve\n"
    title += "hard rules (billing/human_request/unknown_case) REAL; rest STUBBED — see report"
    ax.set_title(title, fontsize=10)
    ax.legend(loc="lower left", fontsize=8)
    ax.grid(alpha=0.3)

    if not approved:
        fig.text(
            0.5,
            0.5,
            "DRAFT\nLABELS NOT YET APPROVED",
            fontsize=30,
            color="red",
            alpha=0.35,
            ha="center",
            va="center",
            rotation=30,
            weight="bold",
        )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def render_report(
    *,
    header: dict[str, Any],
    tickets: list[dict[str, Any]],
    predictions: dict[str, Prediction],
    cm: dict[str, int],
    precision: float,
    recall: float,
    f1: float,
    hard_recall: float | None,
    hard_n: int,
    recommended_threshold: float,
    distribution: dict[str, Any],
    approved: bool,
    image_filename: str,
) -> str:
    # The DRAFT paragraph must quote the status of the file the gate actually
    # consulted, which is always the canonical set — never ``header``, which
    # is whatever --labeled-set pointed at (T-25 acceptance 1).
    approval = canonical_approval_header().get("approval", {})
    banner = report_status_banner(approved)
    lines: list[str] = []
    lines.append("# Escalation eval report — T-7")
    lines.append("")
    lines.append(f"**{banner}**")
    lines.append("")
    if not approved:
        lines.append(
            "> This report is generated end-to-end from `evals/labeled_set.yaml`, but that "
            "file's `approval.status` is "
            f"`{approval.get('status')!r}`, not `APPROVED`. Every number below is a DRAFT — "
            "proof the pipeline runs, not a real measurement. See `evals/REVIEW.md` for what "
            "the project owner needs to review before this can become a final report."
        )
        lines.append("")
    scored_approval = (header.get("approval") or {}).get("status")
    if scored_approval != approval.get("status"):
        lines.append(
            "> NOTE: the scored labeled set was substituted via `--labeled-set` and carries "
            f"`approval.status` `{scored_approval!r}`. The substitution changes only which "
            "tickets were scored below; it has no bearing on the approval banner above, which "
            "reads `evals/labeled_set.yaml` and nothing else."
        )
        lines.append("")
    lines.append(f"Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"Labeled tickets: {distribution['total']}")
    lines.append("")

    lines.append("## Methodology — what is REAL vs STUBBED")
    lines.append("")
    lines.append(
        "No `OPENAI_API_KEY` exists in this environment, and this script assumes no live "
        "Postgres/pgvector connection either, so only pure, deterministic checks are run for "
        "real:"
    )
    lines.append("")
    lines.append("- **REAL** — `rules.is_billing_dispute`, `rules.is_explicit_human_request`: ")
    lines.append("  run directly against each ticket's body text.")
    lines.append(
        "- **REAL** — `rules.is_unknown_case`: a referenced `MFG-####-####` case id is checked "
    )
    lines.append(
        "  for membership in `fixtures/cases.yaml` directly (no live DB) — independent of the "
        "label."
    )
    lines.append(
        "- **STUBBED** — `rules.is_out_of_procedure`, both `low_confidence` subtypes (empty "
    )
    lines.append(
        "  retrieval, verifier failure), and `rules.is_classifier_abstention`: these need a "
        "live"
    )
    lines.append(
        "  permission-matching LLM call, a live KB vector search, a live groundedness judge, or "
    )
    lines.append(
        "  a live classifier call respectively. A small replayable table stands in for the "
        "specific"
    )
    lines.append("  ticket ids authored to represent each scenario — see `evals/report.py`'s ")
    lines.append("  `STUB_STRUCTURAL_REASON` / `STUB_ABSTENTION_IDS`.")
    lines.append(
        "- **STUBBED** — the escalation classifier's frustration/complexity verdict: a "
        "hand-authored,"
    )
    lines.append(
        "  replayable `STUB_CLASSIFIER_VERDICTS` table, a few entries deliberately wrong "
        "relative to"
    )
    lines.append(
        "  the label, so the confusion matrix below demonstrates real disagreement-handling "
        "rather"
    )
    lines.append("  than an uninformative all-correct diagonal.")
    lines.append("")
    lines.append(
        "**No number in this report should be read as a measurement of the real OpenAI-backed "
        "classifier's accuracy.**"
    )
    lines.append("")

    lines.append("## Label distribution")
    lines.append("")
    lines.append(f"- Total tickets: {distribution['total']}")
    lines.append(
        f"- Escalate: {distribution['escalate']} / Not escalate: {distribution['not_escalate']}"
    )
    lines.append("- By route:")
    for route, count in distribution["routes"].items():
        lines.append(f"  - `{route}`: {count}")
    lines.append("- By reason (a ticket can carry more than one):")
    for reason, count in distribution["reasons"].items():
        lines.append(f"  - `{reason}`: {count}")
    lines.append("")

    lines.append("## Confusion matrix (binary escalate / not-escalate, at recommended threshold)")
    lines.append("")
    lines.append("| | Predicted escalate | Predicted no-escalate |")
    lines.append("|---|---|---|")
    lines.append(f"| **Actual escalate** | TP={cm['tp']} | FN={cm['fn']} |")
    lines.append(f"| **Actual no-escalate** | FP={cm['fp']} | TN={cm['tn']} |")
    lines.append("")

    lines.append("## Precision / Recall / F1 (at recommended threshold)")
    lines.append("")
    lines.append(f"- Precision: {precision:.3f}")
    lines.append(f"- Recall: {recall:.3f}")
    lines.append(f"- F1: {f1:.3f}")
    lines.append("")

    lines.append("## Hard-trigger subset recall")
    lines.append("")
    if hard_recall is None:
        lines.append("No hard-trigger tickets in this labeled set.")
    else:
        lines.append(
            f"Recall on the {hard_n} tickets labeled with a genuine DESIGN hard trigger "
            f"(billing/human_request/unknown_case/out_of_procedure/low_confidence, excluding "
            f"frustration/complexity-only escalations): **{hard_recall:.3f}**."
        )
        lines.append(
            "Under DESIGN's OR combinator, a fired hard rule is threshold-independent by "
            "construction, so this number is expected to be 1.0 whenever the predictor "
            "correctly identifies the trigger — it reflects the combinator's design (real for "
            "billing/human_request/unknown_case, stubbed-but-always-firing for "
            "out_of_procedure/low_confidence here), not classifier accuracy."
        )
    lines.append("")

    lines.append("## Recommended threshold")
    lines.append("")
    lines.append(
        f"Sweeping `CLASSIFIER_CONFIDENCE_THRESHOLD` over the STUBBED classifier scores above "
        f"and maximizing F1 recommends **{recommended_threshold:.2f}** "
        f"(current provisional value in `backend/src/escalation/config.py`: "
        f"{CLASSIFIER_CONFIDENCE_THRESHOLD:.2f})."
    )
    lines.append(
        "**This value is NOT written to `backend/src/escalation/config.py`.** Per T-7's own "
        "instructions, choosing a committed threshold requires human-approved labels — this "
        "report computes and states a recommendation only, against labels that are still "
        "`PROPOSED_AWAITING_HUMAN_REVIEW` and against a classifier that is entirely stubbed in "
        "this environment. Treat this number as a starting point for re-running this report "
        "once both are real, not as a number to commit."
    )
    lines.append("")

    lines.append("## PR curve")
    lines.append("")
    lines.append(f"![PR curve]({image_filename})")
    lines.append("")

    lines.append("## Per-ticket signal provenance")
    lines.append("")
    lines.append(
        "Every ticket's REAL vs STUBBED signals, for audit — see `evals/report.py` module "
        "docstring for definitions."
    )
    lines.append("")
    lines.append("| id | expected | predicted | real signals | stubbed signals |")
    lines.append("|---|---|---|---|---|")
    for t in tickets:
        p = predictions[t["id"]]
        exp = "escalate" if t["expected_escalate"] else "no-escalate"
        pred = "escalate" if p.escalate else "no-escalate"
        real = "; ".join(p.real_signals) or "—"
        stub = "; ".join(p.stubbed_signals) or "—"
        mark = "" if exp == pred else " **MISMATCH**"
        lines.append(f"| `{t['id']}` | {exp} | {pred}{mark} | {real} | {stub} |")
    lines.append("")

    lines.append("---")
    lines.append(f"**{banner}**")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labeled-set", type=Path, default=LABELED_SET_PATH)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # The gate reads the committed set; args.labeled_set only drives what is
    # scored and rendered (T-25 acceptance 1 — see canonical_approval_header).
    canonical_header = canonical_approval_header()
    approved = is_approved(canonical_header)

    # Nothing unapproved reaches docs/ — not even watermarked (T-25
    # acceptance 2). A draft render is still available, but the caller has to
    # name an --output-dir outside docs/ and take the non-zero exit with it.
    if not approved and writes_into_docs(args.output_dir):
        print(
            f"REFUSING to write to {args.output_dir}: {LABELED_SET_PATH} is not "
            "human-approved, and docs/ holds published deliverables only. Re-run with "
            "--output-dir pointing outside docs/ for a DRAFT render (still exits non-zero), "
            "or get the labels approved first (see evals/REVIEW.md).",
            file=sys.stderr,
        )
        return 1

    header, tickets = load_labeled_set(args.labeled_set)
    known_case_ids = load_known_case_ids(args.cases)

    sweep = sweep_thresholds(tickets, known_case_ids)
    recommended = recommend_threshold(sweep)
    predictions = run_predictions(tickets, threshold=recommended, known_case_ids=known_case_ids)
    cm = confusion_matrix(tickets, predictions)
    precision, recall, f1 = precision_recall_f1(cm)
    hard_recall, hard_n = hard_trigger_recall(tickets, predictions)
    distribution = label_distribution(tickets)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = plot_pr_curve(sweep, recommended, approved, args.output_dir / "pr_curve.png")

    report_md = render_report(
        header=header,
        tickets=tickets,
        predictions=predictions,
        cm=cm,
        precision=precision,
        recall=recall,
        f1=f1,
        hard_recall=hard_recall,
        hard_n=hard_n,
        recommended_threshold=recommended,
        distribution=distribution,
        approved=approved,
        image_filename=image_path.name,
    )
    (args.output_dir / "report.md").write_text(report_md)

    metrics = {
        "approved": approved,
        # Sourced from the canonical set, not args.labeled_set: a doctored
        # copy must not be able to stamp "APPROVED" into the artifacts either.
        "approval_status": canonical_header.get("approval", {}).get("status"),
        "scored_labeled_set": str(args.labeled_set),
        "scored_labeled_set_is_canonical": args.labeled_set.resolve() == LABELED_SET_PATH.resolve(),
        "confusion_matrix": cm,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "hard_trigger_recall": hard_recall,
        "hard_trigger_subset_size": hard_n,
        "recommended_threshold": recommended,
        "current_provisional_threshold": CLASSIFIER_CONFIDENCE_THRESHOLD,
        "label_distribution": distribution,
    }
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    print(report_md)
    return 0 if approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
