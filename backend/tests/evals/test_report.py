"""T-7: ``evals/report.py`` — run as ``uv run python -m evals.report``
(docs/tickets.json T-7 verify command). Exercised here as a black box (via
subprocess, exactly as it's actually invoked) rather than imported directly,
so this test file has no import dependency on the top-level ``evals``
package — ``mypy backend``'s ``mypy_path`` only covers ``backend/src``, and
keeping this suite black-box avoids coupling backend/tests' typechecking to
a package outside that path.

Also covers the one behavior this ticket is built around: the report
generator must REFUSE to emit a final/authoritative report while
``evals/labeled_set.yaml``'s ``approval.status`` is not ``APPROVED`` —
instead emitting a clearly DRAFT-watermarked report and still exiting 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_LABELED_SET = REPO_ROOT / "evals" / "labeled_set.yaml"
REAL_OUTPUT_DIR = REPO_ROOT / "docs" / "eval-report"


def _run_report(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "evals.report", *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_labeled_set_yaml_is_actually_not_approved_right_now() -> None:
    """Sanity precondition for every other test in this module: if this
    ever fails, it's because a human genuinely approved the labels (see
    evals/REVIEW.md) — not something this suite should silently tolerate
    without the rest of its assertions changing meaning."""
    import yaml

    raw = yaml.safe_load(REAL_LABELED_SET.read_text())
    assert raw["approval"]["status"] != "APPROVED"


def test_report_generator_runs_end_to_end_and_exits_zero() -> None:
    result = _run_report()
    assert result.returncode == 0, result.stderr
    assert (REAL_OUTPUT_DIR / "report.md").exists()
    assert (REAL_OUTPUT_DIR / "pr_curve.png").exists()
    assert (REAL_OUTPUT_DIR / "metrics.json").exists()


def test_pr_curve_image_is_a_real_png() -> None:
    _run_report()
    png_bytes = (REAL_OUTPUT_DIR / "pr_curve.png").read_bytes()
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_bytes) > 1000  # not an empty/placeholder file


def test_metrics_json_has_the_expected_shape() -> None:
    _run_report()
    metrics = json.loads((REAL_OUTPUT_DIR / "metrics.json").read_text())
    for key in (
        "approved",
        "approval_status",
        "confusion_matrix",
        "precision",
        "recall",
        "f1",
        "hard_trigger_recall",
        "hard_trigger_subset_size",
        "recommended_threshold",
        "current_provisional_threshold",
        "label_distribution",
    ):
        assert key in metrics, key
    cm = metrics["confusion_matrix"]
    for key in ("tp", "fp", "fn", "tn"):
        assert key in cm
    total_predicted = cm["tp"] + cm["fp"] + cm["fn"] + cm["tn"]
    assert total_predicted == metrics["label_distribution"]["total"]
    assert 0.0 <= metrics["precision"] <= 1.0
    assert 0.0 <= metrics["recall"] <= 1.0
    assert 0.0 <= metrics["f1"] <= 1.0
    assert 0.0 <= metrics["recommended_threshold"] <= 1.0


def test_report_refuses_a_final_report_while_labels_are_unapproved() -> None:
    """The core "refuse" requirement: against the real (unapproved) fixture,
    the generator must produce a DRAFT-watermarked report, not one that
    reads as an authoritative measurement — and must still exit 0 (this is
    expected, correct behavior, not a failure)."""
    result = _run_report()
    assert result.returncode == 0

    metrics = json.loads((REAL_OUTPUT_DIR / "metrics.json").read_text())
    assert metrics["approved"] is False
    assert metrics["approval_status"] == "PROPOSED_AWAITING_HUMAN_REVIEW"

    report_text = (REAL_OUTPUT_DIR / "report.md").read_text()
    assert "DRAFT" in report_text
    assert "NOT YET APPROVED" in report_text
    assert "FINAL —" not in report_text  # the approved-only banner text


def test_report_would_render_differently_once_labels_are_approved(tmp_path: Path) -> None:
    """Proves the refuse-vs-final branch is a REAL branch, not a constant —
    a synthetic, fully-approved labeled set (isolated in tmp_path, never
    touching the real fixture or its docs/eval-report/ output) must produce
    a report with NO draft watermark and the FINAL banner instead."""
    synthetic_labeled_set = tmp_path / "labeled_set.yaml"
    synthetic_labeled_set.write_text(
        textwrap.dedent(
            """\
            approval:
              status: APPROVED
              approved_by: "Test Reviewer"
              approved_date: "2026-08-13"
            meta: {}
            tickets:
              - id: t-1
                subject: "Routine question"
                body: "How long does extraction usually take?"
                expected_route: kb
                expected_escalate: false
                expected_reasons: []
              - id: t-2
                subject: "Billing problem"
                body: "I was charged twice for my extraction fee, this is a billing error."
                expected_route: escalate
                expected_escalate: true
                expected_reasons: [billing]
            """
        )
    )
    output_dir = tmp_path / "out"

    result = _run_report(
        "--labeled-set",
        str(synthetic_labeled_set),
        "--cases",
        str(REPO_ROOT / "fixtures" / "cases.yaml"),
        "--output-dir",
        str(output_dir),
    )
    assert result.returncode == 0, result.stderr

    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is True

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" not in report_text
    assert "FINAL —" in report_text

    png_bytes = (output_dir / "pr_curve.png").read_bytes()
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_report_still_refuses_when_status_is_approved_but_signoff_fields_are_empty(
    tmp_path: Path,
) -> None:
    """Belt-and-suspenders: ``status: APPROVED`` alone, with no reviewer
    name/date recorded, is not treated as a real approval (see
    ``evals.report.is_approved``) — guards against a status flip that
    isn't actually a completed, attributable human review."""
    synthetic_labeled_set = tmp_path / "labeled_set.yaml"
    synthetic_labeled_set.write_text(
        textwrap.dedent(
            """\
            approval:
              status: APPROVED
              approved_by: ""
              approved_date: ""
            meta: {}
            tickets:
              - id: t-1
                subject: "Routine question"
                body: "How long does extraction usually take?"
                expected_route: kb
                expected_escalate: false
                expected_reasons: []
            """
        )
    )
    output_dir = tmp_path / "out"

    result = _run_report(
        "--labeled-set",
        str(synthetic_labeled_set),
        "--cases",
        str(REPO_ROOT / "fixtures" / "cases.yaml"),
        "--output-dir",
        str(output_dir),
    )
    assert result.returncode == 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text


def test_hard_trigger_recall_is_perfect_for_the_real_hard_rule_signals() -> None:
    """Under DESIGN's OR combinator a fired hard rule is threshold-
    independent by construction, so the report's own hard_trigger_recall
    should be 1.0 against the real labeled set — this also transitively
    confirms the REAL billing/human_request/unknown_case detectors agree
    with every hard-trigger label in evals/labeled_set.yaml (a regression
    here would mean either rules.py changed behavior or a label is wrong)."""
    _run_report()
    metrics = json.loads((REAL_OUTPUT_DIR / "metrics.json").read_text())
    assert metrics["hard_trigger_recall"] == 1.0
    assert metrics["hard_trigger_subset_size"] > 0


def test_classifier_confidence_threshold_is_untouched_by_this_ticket() -> None:
    """T-7's own instructions: CLASSIFIER_CONFIDENCE_THRESHOLD stays
    provisional/untouched — the report only RECOMMENDS a value, never
    writes it to backend/src/escalation/config.py."""
    config_source = (
        REPO_ROOT / "backend" / "src" / "escalation" / "config.py"
    ).read_text()
    assert "CLASSIFIER_CONFIDENCE_THRESHOLD = 0.5" in config_source
    assert "PROVISIONAL" in config_source
