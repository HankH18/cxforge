"""T-16 acceptance 3: the ``pytest_collection_modifyitems`` hook in
``backend/tests/conftest.py`` must ACTUALLY skip every test under
``graph/``, ``grounding/`` and ``portal/`` when ``SKIP_DB_TESTS=1`` — not
merely exist as source that nothing ever exercises.

An adversarial review found exactly that gap in an earlier version of this
ticket: with Postgres reachable locally (as it normally is in dev),
``SKIP_DB_TESTS`` is simply unset when this ticket's own verify command
(``uv run pytest -m "not live" -q``) runs, so the hook's
``if not SKIP_DB_TESTS: return`` short-circuits before its directory list
is ever evaluated — dropping a directory from that list produced *zero*
observable difference in the verify command. Manually reproducing the
scenario the guard exists for (``SKIP_DB_TESTS=1 pytest backend/tests/graph
-q -rs``) showed all tests in the sabotaged directory RAN AND PASSED
instead of being skipped, silently reintroducing the exact "DB tests
silently skip in CI without anyone noticing" failure mode T-16 exists to
close.

This file closes that gap by driving pytest as a real subprocess WITH
``SKIP_DB_TESTS=1`` set — deliberately independent of whether Postgres is
actually reachable, since the hook is only supposed to key off the env var,
never live connectivity — against each of the three directories
individually, and asserting every collected test reports as skipped, with
none reporting as passed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DB_TESTS_DIRS = ("graph", "grounding", "portal")


def _run_with_skip_db_tests(target: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SKIP_DB_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-rs", "-m", "not live"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("subdir", _SKIP_DB_TESTS_DIRS)
def test_skip_db_tests_actually_skips_every_test_in_this_directory(subdir: str) -> None:
    result = _run_with_skip_db_tests(f"backend/tests/{subdir}")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output

    passed_match = re.search(r"(\d+) passed", output)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    assert passed_count == 0, (
        f"expected every test under backend/tests/{subdir} to be SKIPPED (never "
        f"actually run) when SKIP_DB_TESTS=1, but {passed_count} passed:\n{output}"
    )

    skipped_match = re.search(r"(\d+) skipped", output)
    skipped_count = int(skipped_match.group(1)) if skipped_match else 0
    assert skipped_count > 0, (
        f"expected at least one skipped test under backend/tests/{subdir} when "
        f"SKIP_DB_TESTS=1, but none were reported (collection may be broken, or "
        f"the relocation hook no longer covers this directory):\n{output}"
    )

    assert "requires the docker-compose db service" in output, (
        f"skipped tests under backend/tests/{subdir} did not carry the expected "
        f"SKIP_DB_TESTS skip reason:\n{output}"
    )


def test_skip_db_tests_leaves_an_unrelated_directory_unaffected() -> None:
    """Sibling guard: the relocation hook is directory-scoped to exactly
    ``_SKIP_DB_TESTS_DIRS`` — it must not accidentally skip everything when
    SKIP_DB_TESTS=1. ``backend/tests/contract`` has no DB dependency and no
    SKIP_DB_TESTS guard of its own, so it should run and pass exactly as
    normal even with the env var set.

    The sample directory used to be ``backend/tests/hooks``, which
    ``docs/DECISIONS.md`` ADR-019 retired to
    ``.claude/harness-archive/hooks-tests/``. Only the *sample* moved: this
    guard asserts nothing about the harness, and the contract it checks — that
    SKIP_DB_TESTS does not leak outside ``_SKIP_DB_TESTS_DIRS`` — is unchanged
    and still binds. ``contract/`` is a deliberate pick: it drives the
    HelpdeskPort adapters against in-process fakes, so it touches no database
    even when one is unreachable, which is the property this guard needs.
    """
    result = _run_with_skip_db_tests("backend/tests/contract")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output

    passed_match = re.search(r"(\d+) passed", output)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    assert passed_count > 0, (
        f"expected backend/tests/contract to still run and pass with SKIP_DB_TESTS=1 "
        f"set (it has no DB dependency):\n{output}"
    )
