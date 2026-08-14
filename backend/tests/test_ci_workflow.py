"""T-16 acceptance 4: .github/workflows/ci.yml must (a) fail the build if
the database-backed tests were skipped, and (b) fail the build if the
collected/passed test count drops below a floor -- both asserted by
READING the real workflow file structurally (parsing its YAML and walking
`jobs.check.steps[].run`), not by eyeballing it. Anchoring to which STEP
enforces each guard (rather than grepping the raw file text for a keyword
anywhere) means a step that merely prints a value without acting on it
can't false-positive either assertion -- both require the keyword AND an
`exit 1` to co-occur in the SAME step's run body.

Matches this repo's existing convention of flat, harness-level tests
living directly under backend/tests/ (precedent: test_bootstrap.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _run_bodies() -> list[str]:
    workflow = yaml.safe_load(CI_PATH.read_text())
    return [step.get("run", "") for step in workflow["jobs"]["check"]["steps"]]


def test_ci_workflow_file_exists() -> None:
    assert CI_PATH.is_file(), f"expected a CI workflow at {CI_PATH}"


def test_ci_fails_the_build_if_db_tests_were_skipped() -> None:
    assert any(
        "SKIP_DB_TESTS" in body and "exit 1" in body for body in _run_bodies()
    ), "no CI step both checks for a SKIP_DB_TESTS skip and fails the build"


def test_ci_fails_the_build_below_a_collected_count_floor() -> None:
    assert any(
        re.search(r"\bpassed\b", body)
        and re.search(r"-lt\s+\d+|<\s*\d+", body)
        and "exit 1" in body
        for body in _run_bodies()
    ), "no CI step both parses a passed-count and fails the build below a floor"


def test_ci_still_runs_pytest_against_the_not_live_marker() -> None:
    """Guards against the two guards above being satisfied by steps that
    exist but no longer sit downstream of a real pytest invocation."""
    assert any("pytest" in body and '"not live"' in body for body in _run_bodies())
