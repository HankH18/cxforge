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

import pytest

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

# The real evals/labeled_set.yaml was APPROVED by the project owner on
# 2026-08-15. Every test below that needs to exercise the UNAPPROVED path
# therefore builds a synthetic repo whose own canonical set carries this
# header, instead of leaning on the real file's state. That is strictly
# better than what those tests did before: they silently depended on the
# repo happening to be unapproved, so the day a human signed off they all
# went red at once. Pinned to a fixture, the refuse-while-unapproved
# guarantee is testable forever, in either direction.
UNAPPROVED_HEADER = """\
approval:
  status: PROPOSED_AWAITING_HUMAN_REVIEW
  approved_by: ""
  approved_date: ""
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

# T-21: evals/report.py now calls the real, live Anthropic classifier for
# any ticket that reaches EscalationEngine's classifier tier — which is
# nearly every ticket in every fixture below (only billing/human_request
# hard-rule bodies skip it). "The unit suite must not make live API calls"
# (T-21), so every _run_report/_run_report_in call in this module defaults
# to injecting evals.report's TEST-ONLY fixed-verdict escape hatch (see
# that module's docstring) via _base_env below. confidence=1.0 specifically
# (not e.g. 0.99): EscalationEngine.evaluate's own `confidence >= threshold`
# check means anything less than 1.0 lets the F1-maximizing threshold sweep
# push the recommended threshold ABOVE that confidence to suppress the
# classifier tier entirely (empirically confirmed while building this
# fixture) — 1.0 clears every threshold in the [0, 1] sweep, so this
# fixture's behavior does not depend on exactly which threshold gets
# recommended. Tests that need the REAL key path (or no key at all)
# override EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY back to "" explicitly.
DEFAULT_FAKE_LLM_VERDICT = json.dumps(
    {"escalate": True, "reasons": ["frustration"], "confidence": 1.0}
)


def _base_env(extra: dict[str, str | None] | None = None) -> dict[str, str]:
    """``extra`` overrides/adds on top of the default fake-LLM injection —
    a key mapped to ``None`` is REMOVED from the result entirely (not set
    to an empty string), for tests that need a variable genuinely absent
    from the child's environment rather than merely falsy (e.g.
    ``PYTEST_VERSION``, which evals.report._running_under_pytest checks
    for presence, not truthiness)."""
    env = dict(os.environ)
    env.setdefault("EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY", DEFAULT_FAKE_LLM_VERDICT)
    if extra:
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    return env


def _run_report(
    *extra_args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "evals.report", *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=_base_env(env),
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


def _run_report_in(
    repo: Path, *extra_args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = _base_env(env)
    run_env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "backend" / "src"), run_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "evals.report", "--cases", str(REAL_CASES), *extra_args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=run_env,
    )


def _docs_eval_report_snapshot() -> dict[str, bytes]:
    if not REAL_DOCS_EVAL_REPORT.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(REAL_DOCS_EVAL_REPORT.iterdir()) if p.is_file()}


def test_real_labeled_set_approval_state_is_pinned() -> None:
    """Sanity precondition for the rest of this module: whatever state the
    real labeled set is in, the suite knows about it.

    This used to assert `status != APPROVED`, and warned that a failure here
    would mean "a human genuinely approved the labels — not something this
    suite should silently tolerate without the rest of its assertions
    changing meaning." That is exactly what happened (owner sign-off,
    2026-08-15), and the rest of this module's assertions did change with
    it: every test that needs the UNAPPROVED path now builds a synthetic
    repo carrying UNAPPROVED_HEADER rather than depending on the real file.

    What remains worth pinning is that an APPROVED real set is attributed —
    an unattributed approval is the synthetic sign-off T-7's non-goal
    forbids. The shape of the attribution itself is enforced in
    test_labeled_set.py::test_approval_is_attributed_to_a_named_human_with_a_date.
    """
    import yaml

    approval = yaml.safe_load(REAL_LABELED_SET.read_text())["approval"]
    if approval["status"] == "APPROVED":
        assert approval.get("approved_by"), "APPROVED with no approved_by"
        assert approval.get("approved_date"), "APPROVED with no approved_date"


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
        # T-21 acceptance 4 — machine-checkable, not report.md prose.
        "run_timestamp_utc",
        "measured_sample_size",
        "unmeasured_ticket_count",
        "unmeasured_ticket_ids",
        "live_classifier_call_count",
        "predictions",
    ):
        assert key in metrics, key
    cm = metrics["confusion_matrix"]
    for key in ("tp", "fp", "fn", "tn"):
        assert key in cm
    # T-21: the confusion matrix (and therefore precision/recall/f1/
    # hard_trigger_recall) is computed over the MEASURED subset only —
    # out_of_procedure and low_confidence's two structural subtypes are
    # excluded, not defaulted to a guessed answer (see evals/report.py's
    # module docstring point 4). label_distribution still describes the
    # FULL labeled set.
    total_predicted = cm["tp"] + cm["fp"] + cm["fn"] + cm["tn"]
    assert total_predicted == metrics["measured_sample_size"]
    assert (
        metrics["measured_sample_size"] + metrics["unmeasured_ticket_count"]
        == metrics["label_distribution"]["total"]
    )
    assert len(metrics["unmeasured_ticket_ids"]) == metrics["unmeasured_ticket_count"]
    assert 0.0 <= metrics["precision"] <= 1.0
    assert 0.0 <= metrics["recall"] <= 1.0
    assert 0.0 <= metrics["f1"] <= 1.0
    assert 0.0 <= metrics["recommended_threshold"] <= 1.0
    assert metrics["live_classifier_call_count"] >= 0
    # ISO 8601 UTC — datetime.fromisoformat handles the "+00:00" offset
    # datetime.isoformat() produces for a UTC-aware datetime.
    import datetime as _dt

    parsed = _dt.datetime.fromisoformat(metrics["run_timestamp_utc"])
    assert parsed.utcoffset() == _dt.timedelta(0)


def test_report_refuses_a_final_report_while_labels_are_unapproved(tmp_path: Path) -> None:
    """The core "refuse" requirement: while the canonical labeled set is
    unapproved, the generator must produce a DRAFT-watermarked report rather
    than one that reads as an authoritative measurement, and must exit
    non-zero.

    Driven through a synthetic repo whose OWN canonical set is unapproved.
    It previously used the real file and silently depended on the repo
    happening to be unapproved — so the moment the owner signed off, the
    test went red without the guarantee itself having changed. Pinned to a
    fixture, this holds in both directions, forever.
    """
    repo = _synthetic_repo(tmp_path / "repo", UNAPPROVED_HEADER + TWO_TICKETS)
    output_dir = tmp_path / "out"
    result = _run_report_in(repo, "--output-dir", str(output_dir))
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
    should be 1.0 against the real labeled set's MEASURED hard-trigger
    subset (billing/human_request/unknown_case/classifier-abstention —
    T-21's module docstring point 4 excludes out_of_procedure and
    low_confidence's two structural subtypes from this metric entirely,
    not from this assertion's target of 1.0). This transitively confirms
    the REAL billing/human_request/unknown_case detectors agree with every
    hard-trigger label in evals/labeled_set.yaml (a regression here would
    mean either escalation.rules changed behavior or a label is wrong).

    The one classifier-tier ticket in this subset
    (esc-low_confidence-abstention-garbled-01) is driven by
    DEFAULT_FAKE_LLM_VERDICT here, not a real Anthropic call (T-21: "the
    unit suite must not make live API calls") — it always predicts
    escalate=True, so this assertion is agnostic to what the real live
    classifier actually does with a garbled message. See
    ``backend/tests/evals/test_report.py::
    test_report_uses_the_real_anthropic_classifier_when_a_key_is_present``
    (``@pytest.mark.live``) for a genuine, opt-in exercise of the real key
    path, and the T-21 implementation report for the actual measured
    number from a real run.
    """
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
    """T-25 acceptance 1/3: the exact attack the ticket names. A labeled set
    with ``approval.status`` flipped to APPROVED and signoff fields filled in,
    handed to ``--labeled-set``, used to yield a non-draft exit-0 run with no
    human anywhere in the loop. The gate reads the canonical committed file,
    so the substituted set is scored and rendered but the run is still a
    DRAFT and still exits non-zero.

    Run against a synthetic repo whose canonical set is UNAPPROVED. Using the
    real repo would no longer test anything now that the owner has approved
    the real labels: exit 0 would be correct for the canonical file's own
    sake, so a passing run could not distinguish "the flag was ignored" from
    "the flag worked". The attack is only observable while canonical is
    unapproved — which is precisely when it would have mattered.
    """
    import yaml

    repo = _synthetic_repo(tmp_path / "repo", UNAPPROVED_HEADER + TWO_TICKETS)
    doctored = yaml.safe_load((repo / "evals" / "labeled_set.yaml").read_text())
    doctored["approval"] = {
        "status": "APPROVED",
        "approved_by": "Not A Real Reviewer",
        "approved_date": "2026-08-14",
    }
    doctored_path = tmp_path / "doctored_labeled_set.yaml"
    doctored_path.write_text(yaml.safe_dump(doctored, sort_keys=False))
    output_dir = tmp_path / "out"

    result = _run_report_in(
        repo,
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


def test_default_invocation_while_unapproved_writes_nothing_under_docs(
    tmp_path: Path,
) -> None:
    """T-25 acceptance 2: the default invocation (no ``--output-dir``) used to
    render DRAFT artifacts straight into ``docs/eval-report/``. It must refuse
    before writing anything, leaving the published tree byte-identical.

    Runs in a synthetic repo whose canonical set is unapproved, and proves
    non-interference by snapshot comparison rather than by trusting the exit
    code. It previously ran against the real repo's defaults — which was
    correct while the real labels were unapproved, but is now actively wrong:
    with them approved, the default invocation SHOULD write to docs, so
    against the real tree this test would either fail or (worse) pass while
    silently rewriting published artifacts on every suite run.
    """
    repo = _synthetic_repo(tmp_path / "repo", UNAPPROVED_HEADER + TWO_TICKETS)
    docs_dir = repo / "docs" / "eval-report"
    real_docs_before = _docs_eval_report_snapshot()

    result = _run_report_in(repo)

    assert result.returncode != 0
    assert "REFUSING to write" in result.stderr
    assert not docs_dir.exists(), (
        f"unapproved default run created {docs_dir} — nothing unapproved may "
        "reach the published docs tree, not even watermarked"
    )
    # ...and the REAL published tree is untouched, proving the synthetic repo
    # genuinely rooted itself in tmp_path rather than falling back to this one.
    assert _docs_eval_report_snapshot() == real_docs_before


def test_a_draft_render_must_name_an_output_dir_outside_docs(tmp_path: Path) -> None:
    """T-25 acceptance 2, the general rule behind the default case: while the
    canonical set is unapproved, any ``--output-dir`` landing under ``docs/``
    is refused — including a fresh subdirectory that does not exist yet, so
    the refusal demonstrably happens before the directory is created.

    Moved into a synthetic unapproved repo for the same reason as the default
    -invocation test above: against the now-approved real repo this path is
    permitted, so testing it there would assert the opposite of the rule.
    """
    repo = _synthetic_repo(tmp_path / "repo", UNAPPROVED_HEADER + TWO_TICKETS)
    target = repo / "docs" / "eval-report-t25-should-never-exist"
    assert not target.exists(), "stale artifact from a previous run"

    result = _run_report_in(repo, "--output-dir", str(target))
    assert result.returncode != 0
    assert "REFUSING to write" in result.stderr
    assert not target.exists()

    # ...and the same run outside docs/ still produces its draft.
    output_dir = tmp_path / "out"
    assert _run_report_in(repo, "--output-dir", str(output_dir)).returncode != 0
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


# ---------------------------------------------------------------------------
# T-21 — the classifier half now runs against a real live LLMClient. A
# missing/unusable ANTHROPIC_API_KEY must FAIL LOUDLY (write nothing) rather
# than silently substitute a fabricated verdict; a present, working key must
# genuinely reach the real Anthropic API.
# ---------------------------------------------------------------------------


def test_report_fails_loudly_and_writes_nothing_without_a_usable_key(tmp_path: Path) -> None:
    """No ANTHROPIC_API_KEY (and no TEST-ONLY fixed verdict either) must
    refuse the whole run before any file is written — T-21 acceptance 2:
    "without it the report must FAIL LOUDLY rather than silently
    substituting fabricated verdicts". Overrides both env vars back to ""
    explicitly (see ``_base_env``'s default injection above) — the
    synthetic repo below has no ``.env`` of its own for
    ``evals.report._resolve_llm_client`` to fall back to either, so this
    is genuinely "no key anywhere", not merely "no fake"."""
    repo = _synthetic_repo(tmp_path / "repo", APPROVED_HEADER + TWO_TICKETS)
    output_dir = tmp_path / "out"

    result = _run_report_in(
        repo,
        "--output-dir",
        str(output_dir),
        env={"EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY": "", "ANTHROPIC_API_KEY": ""},
    )

    assert result.returncode != 0
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "REFUSING" in result.stderr
    assert not output_dir.exists(), (
        "a run with no usable key must write nothing at all, not even a DRAFT — "
        f"found {output_dir}"
    )


@pytest.mark.live
def test_report_uses_the_real_anthropic_classifier_when_a_key_is_present(tmp_path: Path) -> None:
    """Opt-in (never runs under ``-m "not live"``, matching how this repo's
    only other ``live`` marker — the real Zendesk trial suite — is
    exercised): with a genuine, working ``ANTHROPIC_API_KEY``, the report
    must reach the real Anthropic API rather than failing loudly. Proves
    the positive side of T-21 acceptance 2, complementing
    ``test_report_fails_loudly_and_writes_nothing_without_a_usable_key``'s
    negative side."""
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not available in this environment — see .env.example")

    repo = _synthetic_repo(tmp_path / "repo", APPROVED_HEADER + TWO_TICKETS)
    output_dir = tmp_path / "out"

    result = _run_report_in(
        repo,
        "--output-dir",
        str(output_dir),
        env={"EVALS_REPORT_FAKE_LLM_FOR_TESTS_ONLY": "", "ANTHROPIC_API_KEY": api_key},
    )

    assert result.returncode == 0, result.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text())
    # TWO_TICKETS: t-2 is a billing hard rule (no LLM call); t-1 is a plain
    # kb question that reaches the classifier tier — exactly one real call.
    assert metrics["live_classifier_call_count"] == 1
    assert isinstance(metrics["predictions"]["t-1"]["predicted_escalate"], bool)
