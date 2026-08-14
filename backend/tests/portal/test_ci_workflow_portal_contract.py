"""T-19 acceptance 4: the portal API-contract parity check must be wired
into .github/workflows/ci.yml, asserted by READING the real workflow file
structurally rather than by inspection. Mirrors
backend/tests/test_ci_workflow.py's established pattern (T-16) for the same
file: parse YAML, walk jobs.check.steps[].run bodies, and assert on
co-occurrence of the relevant keywords WITHIN one step's run body (so a
step that merely prints a keyword without acting on it can't
false-positive). That file is out of T-19's scope
(backend/tests/** vs. T-19's backend/tests/portal/**), so this is a
sibling test rather than an edit to it.

No database dependency -- this only reads a YAML file off disk.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _run_bodies() -> list[str]:
    workflow = yaml.safe_load(CI_PATH.read_text())
    return [step.get("run", "") for step in workflow["jobs"]["check"]["steps"]]


def test_ci_runs_the_portal_api_contract_parity_check() -> None:
    assert any(
        "codegen.py" in body and "--check" in body for body in _run_bodies()
    ), "no CI step runs backend/src/portal/codegen.py in --check mode"


def test_ci_builds_and_tests_the_portal() -> None:
    bodies = _run_bodies()
    assert any("npm run build" in body for body in bodies), "no CI step runs `npm run build`"
    assert any("npm test" in body for body in bodies), "no CI step runs `npm test`"
