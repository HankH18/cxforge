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
emitting a clearly DRAFT-watermarked report AND exiting non-zero (T-15:
the approval gate is machine-enforced via ``evals.report.main``'s exit
code, routed through ``is_approved()`` alone — see
``test_report_refuses_a_final_report_while_labels_are_unapproved``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_LABELED_SET = REPO_ROOT / "evals" / "labeled_set.yaml"
REAL_CASES = REPO_ROOT / "fixtures" / "cases.yaml"
REAL_DOCS_EVAL_REPORT = REPO_ROOT / "docs" / "eval-report"

APPROVED_HEADER = """\
approval:
  status: APPROVED
  approved_by: "Test Reviewer"
  approved_date: "2026-08-13"
"""

TWO_TICKETS = """\
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

ONE_TICKET = """\
meta: {}
tickets:
  - id: t-1
    subject: "Routine question"
    body: "How long does extraction usually take?"
    expected_route: kb
    expected_escalate: false
    expected_reasons: []
"""


def _run_report(*extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "evals.report", *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _synthetic_repo(root: Path, labeled_set_yaml: str) -> Path:
    """Build a throwaway repo tree whose OWN ``evals/labeled_set.yaml`` is
    ``labeled_set_yaml``, and return its root.

    T-25 closed the ``--labeled-set`` substitution hole: the approval gate now
    reads the canonical ``<repo>/evals/labeled_set.yaml`` and nothing else, so
    there is no flag and no environment variable that can point it at a
    fixture. The only honest way left to exercise the *approved* direction of
    the gate — without touching the real, deliberately-unapproved file (T-15
    and T-25 both forbid that) — is to give ``report.py`` a different repo to
    be the canonical set of. ``report.py`` derives its root from ``__file__``,
    so a copy under ``root/evals/`` roots itself at ``root``; ``backend/src``
    is supplied via PYTHONPATH because the synthetic tree has no sources of
    its own."""
    (root / "evals").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "evals" / "__init__.py", root / "evals" / "__init__.py")
    shutil.copy(REPO_ROOT / "evals" / "report.py", root / "evals" / "report.py")
    (root / "evals" / "labeled_set.yaml").write_text(labeled_set_yaml)
    return root


def _run_report_in(repo: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "backend" / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "evals.report", "--cases", str(REAL_CASES), *extra_args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _docs_eval_report_snapshot() -> dict[str, bytes]:
    if not REAL_DOCS_EVAL_REPORT.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(REAL_DOCS_EVAL_REPORT.iterdir()) if p.is_file()}


def test_labeled_set_yaml_is_actually_not_approved_right_now() -> None:
    """Sanity precondition for every other test in this module: if this
    ever fails, it's because a human genuinely approved the labels (see
    evals/REVIEW.md) — not something this suite should silently tolerate
    without the rest of its assertions changing meaning."""
    import yaml

    raw = yaml.safe_load(REAL_LABELED_SET.read_text())
    assert raw["approval"]["status"] != "APPROVED"


def test_report_generator_runs_end_to_end_and_exits_zero(tmp_path: Path) -> None:
    """Exit 0 is only true for genuinely approved input, since
    ``evals.report.main`` routes its exit code through ``is_approved()``
    (see ``test_report_refuses_a_final_report_while_labels_are_unapproved``
    for the unapproved case, which exits non-zero). T-25 acceptance 3: the
    approved direction is now exercised by making a synthetic, fully-approved
    fixture be the *canonical* set of a throwaway repo — the real
    ``evals/labeled_set.yaml`` is neither modified nor consulted, and no
    ``--labeled-set`` substitution is involved, because that route can no
    longer reach exit 0 at all."""
    repo = _synthetic_repo(tmp_path / "repo", APPROVED_HEADER + TWO_TICKETS)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
    assert result.returncode == 0, result.stderr
    assert (output_dir / "report.md").exists()
    assert (output_dir / "pr_curve.png").exists()
    assert (output_dir / "metrics.json").exists()


def test_pr_curve_image_is_a_real_png(tmp_path: Path) -> None:
    """Reads the real, committed labeled set/cases (via report.py's own
    defaults — no ``--labeled-set``/``--cases`` override) but generates
    into ``tmp_path``, never ``docs/eval-report/`` (T-16 acceptance 2)."""
    output_dir = tmp_path / "out"
    _run_report("--output-dir", str(output_dir))
    png_bytes = (output_dir / "pr_curve.png").read_bytes()
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_bytes) > 1000  # not an empty/placeholder file


def test_metrics_json_has_the_expected_shape(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    _run_report("--output-dir", str(output_dir))
    metrics = json.loads((output_dir / "metrics.json").read_text())
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


def test_report_refuses_a_final_report_while_labels_are_unapproved(tmp_path: Path) -> None:
    """The core "refuse" requirement: against the real (unapproved) fixture,
    the generator must produce a DRAFT-watermarked report, not one that
    reads as an authoritative measurement — and must still exit 0 (this is
    expected, correct behavior, not a failure)."""
    output_dir = tmp_path / "out"
    result = _run_report("--output-dir", str(output_dir))
    assert result.returncode != 0

    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False
    assert metrics["approval_status"] == "PROPOSED_AWAITING_HUMAN_REVIEW"

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text
    assert "NOT YET APPROVED" in report_text
    assert "FINAL —" not in report_text  # the approved-only banner text


def test_report_would_render_differently_once_labels_are_approved(tmp_path: Path) -> None:
    """Proves the refuse-vs-final branch is a REAL branch, not a constant —
    a synthetic, fully-approved labeled set (canonical to a throwaway repo in
    tmp_path, never touching the real fixture or its docs/eval-report/ output)
    must produce a report with NO draft watermark and the FINAL banner
    instead."""
    repo = _synthetic_repo(tmp_path / "repo", APPROVED_HEADER + TWO_TICKETS)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
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
    isn't actually a completed, attributable human review.

    Driven through the canonical set of a throwaway repo rather than
    ``--labeled-set``: after T-25 the flag cannot reach the gate at all, so a
    flag-driven version of this test would pass no matter what ``is_approved``
    did with the malformed header, i.e. it would stop testing its own name."""
    header = textwrap.dedent(
        """\
        approval:
          status: APPROVED
          approved_by: ""
          approved_date: ""
        """
    )
    repo = _synthetic_repo(tmp_path / "repo", header + ONE_TICKET)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
    assert result.returncode != 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text


def test_report_still_refuses_when_status_is_wrong_but_signoff_fields_are_filled(
    tmp_path: Path,
) -> None:
    """Isolates the ``status == "APPROVED"`` clause of ``is_approved()``:
    a name and date alone (with the status left at its default,
    not-yet-reviewed value) must not be treated as approval — otherwise
    someone could pre-fill signoff fields ahead of an actual review and
    have the gate open regardless of status. Canonical-set driven for the
    reason given in
    ``test_report_still_refuses_when_status_is_approved_but_signoff_fields_are_empty``."""
    header = textwrap.dedent(
        """\
        approval:
          status: PROPOSED_AWAITING_HUMAN_REVIEW
          approved_by: "Test Reviewer"
          approved_date: "2026-08-13"
        """
    )
    repo = _synthetic_repo(tmp_path / "repo", header + ONE_TICKET)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
    assert result.returncode != 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text


def test_report_still_refuses_when_approved_by_is_empty_but_status_and_date_are_correct(
    tmp_path: Path,
) -> None:
    """Isolates the ``bool(approval.get("approved_by"))`` clause of
    ``is_approved()``: status flipped to APPROVED and a date recorded, but
    no reviewer name, must still not count as approval — an attributable
    human has to be on record, not just a status and a timestamp.
    Canonical-set driven for the reason given in
    ``test_report_still_refuses_when_status_is_approved_but_signoff_fields_are_empty``."""
    header = textwrap.dedent(
        """\
        approval:
          status: APPROVED
          approved_by: ""
          approved_date: "2026-08-13"
        """
    )
    repo = _synthetic_repo(tmp_path / "repo", header + ONE_TICKET)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
    assert result.returncode != 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text


def test_report_still_refuses_when_approved_date_is_empty_but_status_and_by_are_correct(
    tmp_path: Path,
) -> None:
    """Isolates the ``bool(approval.get("approved_date"))`` clause of
    ``is_approved()``: status flipped to APPROVED and a reviewer name
    recorded, but no date, must still not count as approval — approval
    has to be timestamped, not just attributed. Canonical-set driven for the
    reason given in
    ``test_report_still_refuses_when_status_is_approved_but_signoff_fields_are_empty``."""
    header = textwrap.dedent(
        """\
        approval:
          status: APPROVED
          approved_by: "Test Reviewer"
          approved_date: ""
        """
    )
    repo = _synthetic_repo(tmp_path / "repo", header + ONE_TICKET)
    output_dir = tmp_path / "out"

    result = _run_report_in(repo, "--output-dir", str(output_dir))
    assert result.returncode != 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text


def test_hard_trigger_recall_is_perfect_for_the_real_hard_rule_signals(tmp_path: Path) -> None:
    """Under DESIGN's OR combinator a fired hard rule is threshold-
    independent by construction, so the report's own hard_trigger_recall
    should be 1.0 against the real labeled set — this also transitively
    confirms the REAL billing/human_request/unknown_case detectors agree
    with every hard-trigger label in evals/labeled_set.yaml (a regression
    here would mean either rules.py changed behavior or a label is wrong)."""
    output_dir = tmp_path / "out"
    _run_report("--output-dir", str(output_dir))
    metrics = json.loads((output_dir / "metrics.json").read_text())
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


# ---------------------------------------------------------------------------
# T-25 — the approval gate reads only the canonical labeled set, and nothing
# unapproved is allowed to land in docs/.
# ---------------------------------------------------------------------------


def test_a_doctored_labeled_set_passed_via_the_flag_cannot_produce_an_exit_zero_run(
    tmp_path: Path,
) -> None:
    """T-25 acceptance 1/3: the exact attack the ticket names. A copy of the
    real labeled set with ``approval.status`` flipped to APPROVED and signoff
    fields filled in, handed to ``--labeled-set``, used to yield a non-draft
    exit-0 run with no human anywhere in the loop. The gate now reads the
    committed file, so the substituted set is scored and rendered but the run
    is still a DRAFT and still exits non-zero."""
    import yaml

    doctored = yaml.safe_load(REAL_LABELED_SET.read_text())
    doctored["approval"] = {
        "status": "APPROVED",
        "approved_by": "Not A Real Reviewer",
        "approved_date": "2026-08-14",
    }
    doctored_path = tmp_path / "doctored_labeled_set.yaml"
    doctored_path.write_text(yaml.safe_dump(doctored, sort_keys=False))
    output_dir = tmp_path / "out"

    result = _run_report(
        "--labeled-set",
        str(doctored_path),
        "--output-dir",
        str(output_dir),
    )
    assert result.returncode != 0, result.stdout

    metrics = json.loads((output_dir / "metrics.json").read_text())
    assert metrics["approved"] is False
    # The artifacts report the canonical status, not the doctored one.
    assert metrics["approval_status"] == "PROPOSED_AWAITING_HUMAN_REVIEW"
    assert metrics["scored_labeled_set_is_canonical"] is False

    report_text = (output_dir / "report.md").read_text()
    assert "DRAFT" in report_text
    assert "FINAL —" not in report_text
    assert "substituted via `--labeled-set`" in report_text


def test_default_invocation_while_unapproved_writes_nothing_under_docs() -> None:
    """T-25 acceptance 2: the default invocation (no ``--output-dir``) used to
    render DRAFT artifacts straight into ``docs/eval-report/``. It must now
    refuse before writing anything, leaving the published tree byte-identical.

    Deliberately runs with the real defaults — that is the invocation the
    ticket is about — and proves non-interference by snapshot comparison
    rather than by trusting the exit code."""
    before = _docs_eval_report_snapshot()
    result = _run_report()
    assert result.returncode != 0
    assert "REFUSING to write" in result.stderr
    assert _docs_eval_report_snapshot() == before


def test_a_draft_render_must_name_an_output_dir_outside_docs(tmp_path: Path) -> None:
    """T-25 acceptance 2, the general rule behind the default case: any
    ``--output-dir`` that lands under ``docs/`` is refused while unapproved,
    including a fresh subdirectory that does not exist yet — the refusal
    happens before the directory is created."""
    target = REPO_ROOT / "docs" / "eval-report-t25-should-never-exist"
    assert not target.exists(), "stale artifact from a previous run"

    result = _run_report("--output-dir", str(target))
    assert result.returncode != 0
    assert "REFUSING to write" in result.stderr
    assert not target.exists()

    # ...and the same run outside docs/ still produces its draft.
    output_dir = tmp_path / "out"
    assert _run_report("--output-dir", str(output_dir)).returncode != 0
    assert "DRAFT" in (output_dir / "report.md").read_text()


def test_an_approved_canonical_set_may_write_into_its_own_docs_tree(tmp_path: Path) -> None:
    """The docs guard is conditioned on approval, not on the path alone: once
    the canonical set is genuinely approved, the default ``docs/eval-report``
    destination works again. Proven in a throwaway repo so the real docs tree
    is never written to."""
    repo = _synthetic_repo(tmp_path / "repo", APPROVED_HEADER + TWO_TICKETS)

    result = _run_report_in(repo)
    assert result.returncode == 0, result.stderr
    assert (repo / "docs" / "eval-report" / "report.md").exists()
    assert "FINAL —" in (repo / "docs" / "eval-report" / "report.md").read_text()
