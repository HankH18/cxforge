"""Root conftest for ``backend/tests`` (T-16: test isolation and suite
hygiene). Because ``testpaths = ["backend/tests"]`` (pyproject.toml), pytest
always imports this file before any nested conftest.py or test module — so
everything at module scope below runs exactly once, at the very start of
every pytest process, before any test connects to the database.

Four independent pieces of hygiene live here, each tied to one T-16
acceptance criterion:

1. Per-process Postgres schema isolation (acceptance 1). The mere fact that
   this file is being imported IS the test-time signal: it is the pytest
   root conftest, never imported in production. It derives one schema name
   per process and sets ``OTHRAM_TEST_SCHEMA`` (``data.db.TEST_SCHEMA_ENV_VAR``)
   before any test can run, then cleans that schema up (and reaps any
   orphaned ones left by a crashed prior run) via the hooks below.
2. A whole-run guard that the suite never leaves ``docs/eval-report``
   dirtier than it found it (acceptance 2), keyed on a CONTENT fingerprint
   rather than ``git status``'s dirty/clean flag — see the comment above
   ``_content_fingerprint`` for why the flag alone is not enough.
3. Relocation of the three inert conftest-level ``pytestmark`` skip guards
   in ``graph/``, ``grounding/`` and ``portal/`` into a
   ``pytest_collection_modifyitems`` hook, which — unlike a sibling
   module's ``pytestmark`` — pytest actually honors for every test file in
   those directories (acceptance 3).
4. A crash-survival reaper that drops orphaned ``test_*`` schemas left
   behind by a pytest process that never got to run its own teardown.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from psycopg import sql

from data.db import TEST_SCHEMA_ENV_VAR, get_connection

REPO_ROOT = Path(__file__).resolve().parents[2]

SKIP_DB_TESTS = os.getenv("SKIP_DB_TESTS") == "1"


# --------------------------------------------------------------------------
# Acceptance 1: derive and pin this process's private schema name.
#
# PYTEST_XDIST_WORKER future-proofs this for xdist (not installed today —
# no new dependency was added for this ticket, so it is always unset in
# practice); the repo-path slug just makes the name legible when debugging
# a leftover schema, it adds no uniqueness of its own. os.getpid() is what
# actually guarantees two concurrent `pytest` invocations never collide:
# two processes always have two different PIDs regardless of worktree.
# setdefault (not a plain assignment) so a human override of
# OTHRAM_TEST_SCHEMA in the environment always wins.
# --------------------------------------------------------------------------


def _derive_test_schema() -> str:
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        return f"test_{worker}"
    slug = hashlib.sha1(str(REPO_ROOT).encode()).hexdigest()[:8]
    return f"test_{slug}_{os.getpid()}"


os.environ.setdefault(TEST_SCHEMA_ENV_VAR, _derive_test_schema())


def _own_schema_name() -> str:
    return os.environ[TEST_SCHEMA_ENV_VAR]


# --------------------------------------------------------------------------
# Acceptance 3: relocate the three inert pytestmark skip guards.
#
# graph/conftest.py, grounding/conftest.py and portal/conftest.py each used
# to declare `pytestmark = pytest.mark.skipif(...)` at conftest module
# scope. pytest only honors a pytestmark declared in a test *module*
# itself (or inherited by a package's __init__.py) — never one merely
# sitting in a sibling conftest.py — so for three directories whose test
# files never repeated that marker themselves, it silently did nothing.
# This hook is collection-time and directory-scoped instead, so it (a)
# actually fires and (b) automatically covers any future test file added
# under these three directories, which is the exact failure mode that
# created the original bug.
# --------------------------------------------------------------------------

_SKIP_DB_TESTS_DIRS = ("graph", "grounding", "portal")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not SKIP_DB_TESTS:
        return
    marker = pytest.mark.skip(
        reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)"
    )
    tests_root = Path(str(config.rootpath)) / "backend" / "tests"
    for item in items:
        try:
            rel = item.path.resolve().relative_to(tests_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in _SKIP_DB_TESTS_DIRS:
            item.add_marker(marker)


# --------------------------------------------------------------------------
# Acceptance 2: the suite must never leave docs/eval-report/ dirtier than
# it found it. `docs/eval-report/report.md` is committed and, at the time
# this ticket was written, already carries pre-existing local edits from
# unrelated work (it embeds a `Generated: <timestamp>` line that differs
# from HEAD on every legitimate evals/report.py invocation) — so the guard
# is a before/after snapshot, not a "must be empty" assertion, to tolerate
# pre-existing dirt while still catching any NEW delta this run itself
# introduces.
#
# That snapshot is a CONTENT fingerprint (hash of every file's bytes under
# docs/eval-report/), not `git status --porcelain`. An earlier version of
# this guard used the porcelain string, and an adversarial review found the
# gap: porcelain only reports a per-path dirty/clean FLAG ("M path"), and
# since report.md is *already* flagged "M" before any test runs, a test
# that silently rewrites its content produces the exact same "M path"
# string both before and after — the flag can't distinguish "still the old
# dirt" from "freshly overwritten by a test that shouldn't have touched
# it". Hashing the actual bytes (and each file's relative path, so an
# add/delete/rename also moves the fingerprint) closes that gap. See
# backend/tests/evals/test_no_docs_writes.py for the regression proof.
# --------------------------------------------------------------------------


def _content_fingerprint(directory: Path) -> str:
    """Content-addressed fingerprint of every file under ``directory``."""
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


_docs_eval_report_baseline: str | None = None


# --------------------------------------------------------------------------
# Session start: capture the docs/eval-report baseline, then reap any
# orphaned test_* schemas left behind by a pytest process that crashed
# before its own teardown fixture ran.
# --------------------------------------------------------------------------


def pytest_sessionstart(session: pytest.Session) -> None:
    global _docs_eval_report_baseline
    _docs_eval_report_baseline = _docs_eval_report_fingerprint()

    if SKIP_DB_TESTS:
        return
    try:
        _reap_orphaned_test_schemas()
    except Exception:
        # Best-effort only — a reaper failure must never block the run or
        # mask real test results.
        pass


def _reap_orphaned_test_schemas() -> None:
    own = _own_schema_name()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            r"WHERE schema_name LIKE 'test\_%' ESCAPE '\'"
        )
        names = [row[0] for row in cur.fetchall()]

    for name in names:
        if name == own:
            continue
        pid_str = name.rsplit("_", 1)[-1]
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass  # confirmed gone -> safe to drop below
        except PermissionError:
            continue  # signal denied -> assume alive, never touch it
        else:
            continue  # no exception -> the pid is alive, leave it alone
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
        except Exception:
            pass  # never let a failed drop take down the run


# --------------------------------------------------------------------------
# Session finish: drop this process's own schema (clean-exit cleanup) and
# assert docs/eval-report/ is exactly as dirty/clean as it was at the start
# of the run.
# --------------------------------------------------------------------------


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not SKIP_DB_TESTS:
        try:
            own = _own_schema_name()
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(own))
                )
        except Exception:
            pass  # cleanup failure must never mask the real test results

    if _docs_eval_report_baseline is None:
        return
    after = _docs_eval_report_fingerprint()
    if after != _docs_eval_report_baseline:
        session.exitstatus = 1
        print(
            "\nT-16 acceptance 2 VIOLATION: docs/eval-report/ content changed "
            f"during this run.\nbefore fingerprint: {_docs_eval_report_baseline!r}"
            f"\nafter fingerprint:  {after!r}"
        )
