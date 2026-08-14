"""T-16 acceptance 2, narrow regression layer: the evals report suite must
never write into ``docs/eval-report``. ``backend/tests/conftest.py``'s
``pytest_sessionfinish`` hook already enforces this for the WHOLE run (see
that file), but this test re-proves it directly and quickly, scoped just
to ``backend/tests/evals`` — narrow (seconds, not the full suite) and it
independently catches a regression the moment someone adds a new
zero-arg ``_run_report()`` call to ``test_report.py`` in the future,
without needing to wait for a full-suite run to notice.

Deliberately drives the real suite via a subprocess rather than importing
and calling test functions directly in-process: that is the only way to
get a clean, isolated before/after snapshot around the run without this
test's own process state (already-imported modules, fixtures) leaking in.

The before/after snapshot is a CONTENT fingerprint (hash of every file's
bytes under docs/eval-report/), deliberately duplicated here from
``backend/tests/conftest.py`` rather than a ``git status --porcelain``
string — an adversarial review found that the porcelain string is BLIND to
a content rewrite of an already-dirty file (report.md embeds a `Generated:
<timestamp>` line that already differs from HEAD before any test runs, so
its "M path" flag reads identically whether or not a test rewrites it
further). ``test_content_fingerprint_catches_a_rewrite_a_status_flag_cant``
below is the regression proof for that exact gap.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

# backend/tests/evals/test_no_docs_writes.py -> evals -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

# The self-recursion trap: running "pytest backend/tests/evals" from
# *inside* a test living in backend/tests/evals would otherwise re-collect
# and re-run this very test, which would try to spawn itself again. Deselect
# it explicitly — pure pytest core (`--deselect`), no plugin needed.
_SELF = "backend/tests/evals/test_no_docs_writes.py::test_evals_suite_leaves_docs_untouched"


def _content_fingerprint(directory: Path) -> str:
    """Content-addressed fingerprint of every file under ``directory``.
    Mirrors ``backend/tests/conftest.py``'s ``_content_fingerprint`` — see
    that file's comment for why this is a byte-hash, not a git-status
    flag."""
    hasher = hashlib.sha256()
    if directory.is_dir():
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                hasher.update(str(path.relative_to(directory)).encode())
                hasher.update(b"\0")
                hasher.update(path.read_bytes())
                hasher.update(b"\0")
    return hasher.hexdigest()


def _docs_eval_report_fingerprint() -> str:
    return _content_fingerprint(REPO_ROOT / "docs" / "eval-report")


def test_evals_suite_leaves_docs_untouched() -> None:
    before = _docs_eval_report_fingerprint()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/evals",
            "-q",
            "-m",
            "not live",
            "--deselect",
            _SELF,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    after = _docs_eval_report_fingerprint()
    assert after == before, (
        "backend/tests/evals left docs/eval-report/ with different content than "
        f"it found it.\nbefore fingerprint: {before!r}\nafter fingerprint:  {after!r}"
    )


def test_content_fingerprint_catches_a_rewrite_a_status_flag_cant(tmp_path: Path) -> None:
    """Regression proof for the exact gap an adversarial review found in an
    earlier version of this guard, which compared `git status --porcelain
    -- docs/eval-report` as a before/after STRING. `docs/eval-report/
    report.md` is a tracked file that is already dirty (flagged "M") before
    this suite ever runs — it embeds a `Generated: <timestamp>` line that
    differs from HEAD on every legitimate ``evals/report.py`` invocation —
    so `git status --porcelain` reports the SAME "M path" line both before
    and after a test silently rewrites the file's content: the flag cannot
    distinguish "still the same old dirt" from "freshly overwritten by a
    test that shouldn't have touched it".

    Reproduces that exact shape in an isolated ``tmp_path`` directory
    (never the real, concurrently-shared docs/eval-report/) using the
    ACTUAL ``_content_fingerprint`` function this file relies on, and
    proves it distinguishes two different rewrites of an already-dirty
    file even though a naive status-flag string cannot.
    """
    watched = tmp_path / "eval-report"
    watched.mkdir()
    report = watched / "report.md"
    report.write_text("Generated: 2026-08-14T06:40:43Z\nsome report body\n")

    def _naive_status_flag() -> str:
        # Mirrors `git status --porcelain -- docs/eval-report`: a dirty
        # tracked file reports only "M <path>", never its content — so
        # this is deliberately blind to what report.md's bytes actually
        # say, exactly like the guard this fingerprint replaced.
        return "M eval-report/report.md\n" if report.exists() else ""

    # State A: dirty with one rewrite.
    status_a = _naive_status_flag()
    fingerprint_a = _content_fingerprint(watched)

    # State B: dirty with a DIFFERENT rewrite (e.g. a second real
    # evals/report.py invocation with a later `Generated:` timestamp) —
    # same tracked path, same "M" flag, genuinely different bytes.
    report.write_text("Generated: 2026-08-14T12:24:28Z\nsome report body\n")
    status_b = _naive_status_flag()
    fingerprint_b = _content_fingerprint(watched)

    assert status_a == status_b, (
        "test premise broken: the naive status-flag string was expected to read "
        f"identically for two different rewrites of an already-dirty file, but "
        f"didn't: {status_a!r} != {status_b!r}"
    )
    assert fingerprint_a != fingerprint_b, (
        "content fingerprint failed to distinguish two different rewrites of an "
        "already-dirty file even though the git-status-style flag treats them "
        "identically — this is exactly the blind spot an adversarial review found."
    )
