"""W1-E3 acceptance — ``evals/route_accuracy.py``, black-box.

WHY SUBPROCESS AND NOT AN IMPORT
--------------------------------
Two independent reasons, and the second is the load-bearing one:

1. ``evals/`` lives outside ``backend/src``, which is the only path
   ``uv run mypy backend`` resolves — the same reason
   ``backend/tests/evals/test_report.py`` and ``test_no_divergence.py`` already
   drive their subject as a subprocess.
2. It keeps this directory's first-party import roots unchanged
   (``data``/``agent``/``escalation``/``helpdesk``), which is what
   ``docs/BUILD-PLAN.md §3`` warns about for ``backend/tests/worker/``.

   On (2), the constraint is **retired**, though not for the reason an earlier
   revision of this docstring gave. It was not removed by ``d8f3858``; that
   commit is unrelated and ``backend/tests/plan/`` is still present in ``HEAD``.
   It was moved to ``.claude/harness-archive/plan-tests/`` by owner decision
   ``docs/DECISIONS.md`` **ADR-018**, which retired the plan-integrity suite along
   with the ticket harness it tested. ``docs/BUILD-PLAN.md §10.1`` records the
   resolution. This module keeps the subprocess form regardless, because (1)
   requires it anyway; nobody should re-derive a placement constraint from here.

NO LIVE CALLS IN THE GATED SUITE
--------------------------------
Everything here uses ``EVALS_ROUTE_ACCURACY_FAKE_LLM_FOR_TESTS_ONLY``, the
harness's TEST-ONLY canned-``Classification`` hatch. ``test_fake_llm_hatch_is
_refused_outside_a_pytest_process`` is the structural proof that the hatch
cannot leak into a real measurement. The one test that genuinely spends money is
marked ``live`` and is excluded by ``-m "not live"``.

Every file this module writes goes under ``tmp_path`` — ``backend/tests/
conftest.py`` diffs ``git status --porcelain`` across the session and fails the
run otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_LLM_ENV_VAR = "EVALS_ROUTE_ACCURACY_FAKE_LLM_FOR_TESTS_ONLY"

# The labeled set's own shape, asserted rather than assumed: 51 tickets, of
# which 30 carry one of the four branch routes classify can actually emit and 21
# are labeled `escalate` (which classify structurally cannot emit — its schema
# is agent.state.ClassifyRoute). If evals/labeled_set.yaml changes, these
# numbers must be re-derived deliberately, not silently absorbed.
EXPECTED_TOTAL = 51
EXPECTED_SCORED = 30
EXPECTED_ESCALATE = 21

# The escalations whose detection lives inside one specific branch node, so a
# mis-route means the ticket does not escalate at all. Pinned here so a change
# to evals/route_accuracy.required_branch_route has to be made on purpose.
EXPECTED_ROUTE_DEPENDENT = {
    "esc-unknown_case-nonexistent-id-01": "case_status",
    "esc-unknown_case-nonexistent-id-02": "case_status",
    "esc-out_of_procedure-change-requester-01": "permission",
    "esc-out_of_procedure-early-deletion-01": "permission",
    "esc-low_confidence-empty_retrieval-accreditation-01": "kb",
    "esc-low_confidence-empty_retrieval-international-01": "kb",
    "esc-low_confidence-verifier_failure-exact-date-01": "kb",
    "esc-low_confidence-verifier_failure-summed-timeline-01": "kb",
}


def _fake(route: str) -> str:
    return json.dumps(
        {"default": {"topic": "canned", "route": route, "confidence": 0.5, "case_id": None}}
    )


def _run(
    output_dir: Path,
    *,
    fake_route: str | None = "kb",
    extra_args: list[str] | None = None,
    env_overrides: dict[str, str] | None = None,
    drop_pytest_marker: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_route is not None:
        env[FAKE_LLM_ENV_VAR] = _fake(fake_route)
    else:
        env.pop(FAKE_LLM_ENV_VAR, None)
    if drop_pytest_marker:
        # PYTEST_VERSION is the second, independent signal the harness requires
        # before honouring the fake hatch. Removing it simulates a real shell.
        env.pop("PYTEST_VERSION", None)
        env.pop("PYTEST_CURRENT_TEST", None)
    env.update(env_overrides or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.route_accuracy",
            "--output-dir",
            str(output_dir),
            *(extra_args or []),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _results(output_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((output_dir / "results.json").read_text())
    return payload


def test_scores_only_the_branch_route_tickets(tmp_path: Path) -> None:
    """The 21 ``escalate`` labels must not be graded against a route classify
    cannot emit — doing so would mark them all wrong by construction and drag
    the headline number to a meaningless 30/51."""
    out = tmp_path / "run"
    proc = _run(out)
    assert proc.returncode == 0, proc.stderr

    payload = _results(out)
    summary = payload["summary"]
    assert payload["tickets_considered"] == EXPECTED_TOTAL
    assert summary["scored_sample_size"] == EXPECTED_SCORED
    assert summary["escalate_diagnostics"]["total"] == EXPECTED_ESCALATE
    assert len([r for r in payload["results"] if not r["scored"]]) == EXPECTED_ESCALATE


def test_confusion_matrix_records_the_actual_misroutes(tmp_path: Path) -> None:
    """A model that answers ``kb`` for everything must score 10/30 with all 20
    misses landing in the ``kb`` column — not a blanket pass, and not a blanket
    fail."""
    out = tmp_path / "run"
    proc = _run(out, fake_route="kb")
    assert proc.returncode == 0, proc.stderr

    summary = _results(out)["summary"]
    matrix = summary["confusion_matrix"]

    assert summary["route_accuracy"] == round(10 / 30, 4)
    assert matrix["kb"]["kb"] == 10
    assert matrix["case_status"]["kb"] == 10
    assert matrix["permission"]["kb"] == 5
    assert matrix["off_topic"]["kb"] == 5
    assert matrix["case_status"]["case_status"] == 0

    per_route = summary["per_route"]
    assert per_route["kb"]["recall"] == 1.0
    assert per_route["kb"]["precision"] == round(10 / 30, 4)
    assert per_route["case_status"]["recall"] == 0.0


def test_route_dependent_escalations_are_identified_and_graded(tmp_path: Path) -> None:
    """The escalate tickets are diagnostic, EXCEPT the ones whose escalation is
    produced inside one branch node. Those get a real pass/fail, because a
    mis-route there means the ticket silently never escalates."""
    out = tmp_path / "run"
    proc = _run(out, fake_route="kb")
    assert proc.returncode == 0, proc.stderr

    payload = _results(out)
    esc = payload["summary"]["escalate_diagnostics"]
    assert esc["route_dependent_total"] == len(EXPECTED_ROUTE_DEPENDENT)

    by_id = {
        r["ticket_id"]: r["required_branch_route"]
        for r in payload["results"]
        if r["required_branch_route"] is not None
    }
    assert by_id == EXPECTED_ROUTE_DEPENDENT

    # The canned client answers "kb" for everything, so exactly the three
    # kb-required tickets pass and the five others are reported as misses.
    assert esc["route_dependent_correct"] == sum(
        1 for route in EXPECTED_ROUTE_DEPENDENT.values() if route == "kb"
    )
    missed = {m["id"] for m in esc["route_dependent_misses"]}
    assert missed == {i for i, r in EXPECTED_ROUTE_DEPENDENT.items() if r != "kb"}


def test_refuses_to_write_under_docs(tmp_path: Path) -> None:
    """``docs/eval-report/`` is an approved, published artifact and
    ``backend/tests/conftest.py`` fingerprints it across every run. The harness
    must refuse the whole docs/ tree rather than rely on anyone remembering."""
    proc = _run(REPO_ROOT / "docs" / "route-accuracy-should-not-exist")
    assert proc.returncode != 0
    assert "refusing to write under docs/" in (proc.stderr + proc.stdout)
    assert not (REPO_ROOT / "docs" / "route-accuracy-should-not-exist").exists()


def test_fake_llm_hatch_is_refused_outside_a_pytest_process(tmp_path: Path) -> None:
    """The canned-``Classification`` hatch must need TWO signals, not one.

    Gated on the env var alone it would be convention, not structure: a leaked
    export would turn a real measurement into fabricated numbers with nothing in
    the artifacts saying so. Same defect, and the same fix, as
    ``evals/report.py``'s ``_running_under_pytest``.
    """
    out = tmp_path / "run"
    proc = _run(out, fake_route="kb", drop_pytest_marker=True)
    assert proc.returncode != 0
    assert "not a pytest process" in proc.stderr
    assert not (out / "results.json").exists()


def test_missing_credentials_fail_loudly_rather_than_measuring_nothing(tmp_path: Path) -> None:
    """With no hatch and no key, the harness must stop. E3 exists to measure the
    real model; a silent fallback to anything else would be worse than no
    number."""
    out = tmp_path / "run"
    proc = _run(
        out,
        fake_route=None,
        env_overrides={"ANTHROPIC_API_KEY": "", "HOME": str(tmp_path)},
        extra_args=["--limit", "1"],
    )
    assert proc.returncode != 0
    assert "ANTHROPIC_API_KEY is not set" in proc.stderr
    assert not (out / "results.json").exists()


def test_cache_replays_instead_of_re_measuring(tmp_path: Path) -> None:
    """A re-run must not silently cost another full sweep. Checkpointing is what
    makes a 51-ticket live sweep safe to re-run and safe to interrupt."""
    out = tmp_path / "run"
    first = _run(out)
    assert first.returncode == 0, first.stderr
    assert _results(out)["live_call_count"] == EXPECTED_TOTAL
    assert _results(out)["cache_hit_count"] == 0

    second = _run(out)
    assert second.returncode == 0, second.stderr
    assert _results(out)["live_call_count"] == 0
    assert _results(out)["cache_hit_count"] == EXPECTED_TOTAL

    refreshed = _run(out, extra_args=["--refresh"])
    assert refreshed.returncode == 0, refreshed.stderr
    assert _results(out)["live_call_count"] == EXPECTED_TOTAL


def test_a_fake_cache_can_never_be_read_back_by_a_real_measurement(tmp_path: Path) -> None:
    """The cache file records its provenance, and provenance is part of every
    key. Without that, a test run could poison the cache a live sweep later
    reads — reintroducing fabricated numbers through the back door."""
    out = tmp_path / "run"
    assert _run(out).returncode == 0

    cache = json.loads((out / "cache.json").read_text())
    assert cache["provenance"] == "fake"
    assert cache["entries"]
    for key in cache["entries"]:
        assert json.loads(key)["provenance"] == "fake"


def test_limit_bounds_the_sweep(tmp_path: Path) -> None:
    out = tmp_path / "run"
    proc = _run(out, extra_args=["--limit", "4"])
    assert proc.returncode == 0, proc.stderr
    payload = _results(out)
    assert payload["tickets_considered"] == 4
    assert payload["live_call_count"] == 4


def test_min_accuracy_gate_fails_on_a_bad_sweep(tmp_path: Path) -> None:
    out = tmp_path / "run"
    proc = _run(out, fake_route="kb", extra_args=["--min-accuracy", "0.9"])
    assert proc.returncode == 1
    assert "below --min-accuracy" in proc.stdout


def test_report_markdown_carries_the_matrix_and_the_misses(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert _run(out, fake_route="kb").returncode == 0
    report = (out / "report.md").read_text()
    assert "## Confusion matrix" in report
    assert "Route accuracy" in report
    assert "Route-dependent misses" in report
    assert "esc-unknown_case-nonexistent-id-01" in report


@pytest.mark.live
def test_live_route_accuracy_smoke(tmp_path: Path) -> None:
    """One real claude-opus-5 call through the shipped ``agent.nodes.classify``.

    Excluded from the gated suite by ``-m "not live"``; this is the anchor that
    the offline tests above are testing a harness that genuinely works against
    the real model, not just against its own double.
    """
    out = tmp_path / "run"
    proc = _run(out, fake_route=None, extra_args=["--limit", "1"])
    if proc.returncode != 0 and "ANTHROPIC_API_KEY is not set" in proc.stderr:
        pytest.skip("no ANTHROPIC_API_KEY — run with `set -a; source .env; set +a`")
    assert proc.returncode == 0, proc.stderr

    payload = _results(out)
    assert payload["live_call_count"] == 1
    assert payload["model"] == "claude-opus-5"
    assert payload["cost"]["usd"] is not None and payload["cost"]["usd"] > 0
    assert payload["results"][0]["predicted_route"] in (
        "case_status",
        "permission",
        "kb",
        "off_topic",
    )
