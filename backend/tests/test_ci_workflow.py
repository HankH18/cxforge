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
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _run_bodies() -> list[str]:
    workflow = yaml.safe_load(CI_PATH.read_text())
    return [step.get("run", "") for step in workflow["jobs"]["check"]["steps"]]


def _step_run_by_name(name: str) -> str:
    workflow = yaml.safe_load(CI_PATH.read_text())
    for step in workflow["jobs"]["check"]["steps"]:
        if step.get("name") == name:
            body: str = step.get("run", "")
            assert body.strip(), f"CI step {name!r} has an empty run body"
            return body
    raise AssertionError(f"no CI step named {name!r}")


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


# --------------------------------------------------------------------------
# The guards above only assert that a step MENTIONS SKIP_DB_TESTS and fails
# the build. That is satisfied by a matcher that fires on everything as
# happily as by one that fires on the right thing -- and for the whole life
# of the workflow it was the former. `grep -qi "SKIP_DB_TESTS"` matched the
# ordinary progress line for THIS DIRECTORY's own
# test_skip_db_tests_relocation.py, so run 32003095488 failed the guard with
# 511 tests passed against a reachable Postgres and nothing skipped at all.
#
# So the two tests below execute the real step body out of the real YAML --
# not a copy of its regex -- against pytest output captured verbatim from
# real runs, and assert the exit code both ways round. A guard that cannot
# fire is worse than no guard; a guard that always fires is what these pin
# shut.
# --------------------------------------------------------------------------

_DB_SKIP_GUARD_STEP = "Assert database tests actually ran"

# The one reason string every database skip in the suite uses: the
# `pytest_collection_modifyitems` hook in backend/tests/conftest.py and all of
# the module-level `skipif` sites under data/, graph/, grounding/, ingress/
# and portal/. Kept as a constant so these fixtures cannot drift from it
# silently -- if the suite's reason text ever changes, change it here too and
# the guard's matcher with it.
_DB_SKIP_REASON = "requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)"

# Verbatim shapes from `pytest -m "not live" -rs` (pytest 9.1.1). Note the two
# forms of the SKIPPED summary line: no line number when the skip came from the
# collection hook, a line number when it came from a `skipif` or an in-body
# `pytest.skip()`.
_PROGRESS_LINE_DECOY = (
    "backend/tests/test_skip_db_tests_relocation.py ....                      [  3%]\n"
)
_SUMMARY_HEADER = (
    "=========================== short test summary info " + "============================\n"
)

# What CI actually produced in run 32003095488: the decoy progress line, one
# legitimate non-database skip, and 511 passing tests.
_PYTEST_OUT_NO_DB_SKIP = (
    "============================= test session starts ==============================\n"
    "collected 514 items / 2 deselected / 512 selected\n"
    "\n"
    "backend/tests/test_ci_workflow.py ....                                   [  2%]\n"
    + _PROGRESS_LINE_DECOY
    + "backend/tests/ingress/test_queue_contract.py .........s..              [ 15%]\n"
    "\n"
    + _SUMMARY_HEADER
    + "SKIPPED [1] backend/tests/ingress/test_queue_contract.py:227: "
    "no .env in this checkout (CI); nothing for load_dotenv to read\n"
    "============ 511 passed, 1 skipped, 2 deselected in 148.02s ============\n"
)

# A real database skip -- and the decoy is still present, so this also proves
# the guard fires on the REASON rather than on the absence of the decoy.
_PYTEST_OUT_WITH_DB_SKIP = (
    "============================= test session starts ==============================\n"
    "collected 30 items\n"
    "\n"
    "backend/tests/graph/test_tracing.py ssssssssssssss                       [ 46%]\n"
    + _PROGRESS_LINE_DECOY
    + "\n"
    + _SUMMARY_HEADER
    + f"SKIPPED [8] backend/tests/graph/test_tracing.py: {_DB_SKIP_REASON}\n"
    + f"SKIPPED [1] backend/tests/data/test_lookup.py:27: {_DB_SKIP_REASON}\n"
    "==================== 30 skipped, 2 deselected in 0.05s =====================\n"
)


def _run_db_skip_guard(pytest_out: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the guard step's real shell body over ``pytest_out``.

    Only the hard-coded ``/tmp/pytest.out`` path is substituted, so the matcher
    under test is whatever is committed in ci.yml. ``bash -e`` mirrors the
    default shell GitHub Actions runs a ``run:`` body with.
    """
    body = _step_run_by_name(_DB_SKIP_GUARD_STEP)
    assert "/tmp/pytest.out" in body, (
        "the db-skip guard no longer reads /tmp/pytest.out; this test can no "
        "longer redirect it at a fixture"
    )
    out_file = tmp_path / "pytest.out"
    out_file.write_text(pytest_out)
    return subprocess.run(
        ["bash", "-e", "-c", body.replace("/tmp/pytest.out", str(out_file))],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_db_skip_guard_ignores_a_module_merely_named_after_the_variable(tmp_path: Path) -> None:
    result = _run_db_skip_guard(_PYTEST_OUT_NO_DB_SKIP, tmp_path)
    assert result.returncode == 0, (
        "the db-skip guard failed a run in which no database test was skipped. "
        "It is matching a bare substring (a filename, path or test id) instead "
        f"of the skip reason `-rs` prints.\n{result.stdout}\n{result.stderr}"
    )


def test_db_skip_guard_fires_on_a_real_db_skip_reason(tmp_path: Path) -> None:
    result = _run_db_skip_guard(_PYTEST_OUT_WITH_DB_SKIP, tmp_path)
    assert result.returncode != 0, (
        "the db-skip guard passed output containing genuine SKIPPED lines with "
        f"the reason {_DB_SKIP_REASON!r}. CI would ship a green tick over a "
        f"database layer that never ran.\n{result.stdout}\n{result.stderr}"
    )


def test_db_skip_guard_fires_even_when_pytest_colours_its_output(tmp_path: Path) -> None:
    """pytest wraps the word SKIPPED in an ANSI SGR pair when colour is on:

        \x1b[33mSKIPPED\x1b[0m [1] backend/tests/data/test_lookup.py:27: ...

    which puts an escape between the line start and the token. A matcher
    anchored with `^SKIPPED` therefore matches ZERO lines the moment anything
    turns colour on -- a FORCE_COLOR/PY_COLORS in the environment, a future
    runner image, a `--color=yes` added for readability -- and a guard that
    cannot fire is worse than no guard. ci.yml pins `--color=no` on the pytest
    invocation, but this asserts the matcher does not *depend* on that.
    """
    coloured = _PYTEST_OUT_WITH_DB_SKIP.replace("SKIPPED", "\x1b[33mSKIPPED\x1b[0m")
    assert "\x1b[33mSKIPPED\x1b[0m [8]" in coloured, "fixture did not get coloured"

    result = _run_db_skip_guard(coloured, tmp_path)
    assert result.returncode != 0, (
        "the db-skip guard stopped firing once pytest coloured the SKIPPED "
        "token, so it is anchored to the start of the line rather than to the "
        f"summary line's structure.\n{result.stdout}\n{result.stderr}"
    )


# --------------------------------------------------------------------------
# The Lint step -- same failure shape as the guard above, one step earlier.
# `ruff`'s `extend-exclude` in pyproject.toml is gitignore-style, so its bare
# `"portal"` entry matches any directory of that name at ANY depth: it swallows
# backend/src/portal/ (8 files) and backend/tests/portal/ (11), which are
# Python, not the Vite app. `uv run ruff check .` reports 124 files and none of
# those 19, so CI linted none of them for the entire life of the workflow while
# looking exactly like a lint gate.
# `.claude/rules/build-protocol.md` rule 2 has always required the explicit
# second invocation before a local commit; CI was the weaker gate.
#
# So this measures COVERAGE by running the step's real command lines with
# `--show-files`, rather than grepping the step for a path. It stays honest if
# someone narrows the exclude pattern properly and drops the explicit paths,
# and it goes red if a new directory falls into the same trap.
# --------------------------------------------------------------------------

# `.claude/` is the one tree whose Python is excluded on purpose: the retired
# build harness and `.claude/harness-archive/`, kept as a historical record
# (docs/DECISIONS.md ADR-001, ADR-018, ADR-019) and not maintained code.
_DELIBERATELY_UNLINTED = (".claude/",)


def _tracked_python_files() -> set[str]:
    """Tracked, so an untracked scratch file cannot fail the build."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    tracked = {path for path in out.split("\0") if path}
    assert len(tracked) > 100, f"only {len(tracked)} tracked .py files found; git ls-files failed?"
    return {path for path in tracked if not path.startswith(_DELIBERATELY_UNLINTED)}


def _files_the_lint_step_checks() -> set[str]:
    """Every file the Lint step's ruff invocations actually visit.

    `--show-files` lists what ruff *would* check and checks nothing, so this
    measures the step's reach without depending on the code being clean.
    """
    body = _step_run_by_name("Lint")
    lines = [line.split("#", 1)[0] for line in body.splitlines()]
    invocations = [line for line in lines if "ruff" in line and "check" in line]
    assert invocations, "the Lint step no longer runs `ruff check`"

    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    assert Path(ruff).is_file(), f"no ruff executable found at {ruff}"

    checked: set[str] = set()
    for line in invocations:
        args = shlex.split(line)
        targets = args[args.index("check") + 1 :]
        result = subprocess.run(
            [ruff, "check", *targets, "--show-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"`ruff check {' '.join(targets)} --show-files` failed:\n{result.stderr}"
        )
        for path in result.stdout.splitlines():
            if path.strip():
                checked.add(Path(path.strip()).resolve().relative_to(REPO_ROOT).as_posix())
    return checked


def test_the_lint_step_checks_every_tracked_python_file() -> None:
    unchecked = sorted(_tracked_python_files() - _files_the_lint_step_checks())
    assert not unchecked, (
        "CI's Lint step never checks these tracked Python files:\n  "
        + "\n  ".join(unchecked)
        + "\nruff's extend-exclude is gitignore-style, so a bare directory name "
        "in it matches at any depth. Either narrow the pattern in "
        "pyproject.toml or name the paths explicitly in the Lint step."
    )
