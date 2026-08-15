"""Root conftest for ``backend/tests`` (T-16: test isolation and suite
hygiene). Because ``testpaths = ["backend/tests"]`` (pyproject.toml), pytest
always imports this file before any nested conftest.py or test module — so
everything at module scope below runs exactly once, at the very start of
every pytest process, before any test connects to the database.

Five independent pieces of hygiene live here, each tied to a T-16 or T-23
acceptance criterion:

1. Per-process Postgres schema isolation (acceptance 1). The mere fact that
   this file is being imported IS the test-time signal: it is the pytest
   root conftest, never imported in production. It derives one schema name
   per process and sets ``OTHRAM_TEST_SCHEMA`` (``data.db.TEST_SCHEMA_ENV_VAR``)
   before any test can run, then cleans that schema up (and reaps any
   orphaned ones left by a crashed prior run) via the hooks below.
2. A whole-run guard that the suite never leaves ``docs/eval-report``
   dirtier than it found it (T-16 acceptance 2 / T-23 acceptance 4), keyed
   on a CONTENT fingerprint rather than ``git status``'s dirty/clean flag —
   see the comment above ``_content_fingerprint`` for why the flag alone is
   not enough. T-23 acceptance 4 requires this to be kept, not weakened or
   subsumed: a directory-scoped byte fingerprint catches an already-dirty
   file being silently rewritten (same "M" flag, different bytes), which
   the whole-tree check below — being ``git status``-based — cannot.
3. A whole-REPO-TREE guard (T-23 acceptance 2): ``git status --porcelain``
   must report the same set of dirty lines at session finish as it did at
   session start, i.e. the suite added, modified, or removed nothing.
   Pre-existing dirt anywhere in the tree is tolerated via
   snapshot-before-suite comparison, never by exempting a whole directory.
   A short, explicit, named list of paths written by the HARNESS (not the
   test suite) during a live session is excluded — see
   ``_HARNESS_WRITTEN_PATHS`` for exactly which paths and why.
4. Relocation of the three inert conftest-level ``pytestmark`` skip guards
   in ``graph/``, ``grounding/`` and ``portal/`` into a
   ``pytest_collection_modifyitems`` hook, which — unlike a sibling
   module's ``pytestmark`` — pytest actually honors for every test file in
   those directories (acceptance 3).
5. A crash-survival reaper that drops orphaned ``test_*`` schemas left
   behind by a pytest process that never got to run its own teardown.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
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
# T-23 acceptance 2: whole-repo-tree cleanliness, by snapshot-before-suite
# comparison of `git status --porcelain` rather than a "must be empty"
# assertion — this repo, like docs/eval-report/ above, can carry
# pre-existing local dirt anywhere in the tree, and that dirt is not this
# suite's fault. Snapshotting once at session start and once at session
# finish and diffing the two catches exactly what the suite itself did:
#
#   * a path with no line in the baseline that has one at finish -> ADDED
#     or newly-modified (a clean tracked file can only gain a status line
#     by being touched; an untracked file can only gain one by being
#     created).
#   * a path with a line in the baseline that has none at finish -> REMOVED
#     or reverted to clean.
#   * a path whose status LINE itself differs (e.g. " M path" baseline vs.
#     "MM path" finish, meaning the worktree copy was touched again on top
#     of a pre-existing staged change) -> also caught, because we diff
#     whole porcelain lines, not bare paths.
#
# This is deliberately independent of, not a replacement for, the
# docs/eval-report content fingerprint above (T-23 acceptance 4): `git
# status` reports only a per-path dirty/clean FLAG, so it is blind to a
# rewrite of an already-dirty file that preserves the same flag (see that
# section's comment, and the regression proof
# test_content_fingerprint_catches_a_rewrite_a_status_flag_cant in
# backend/tests/evals/test_no_docs_writes.py). The fingerprint stays as the
# strong, content-addressed check for the one directory known to already be
# dirty at HEAD; this tree-wide check is the coarser, whole-repo complement
# acceptance 2 asks for. Keeping both is what acceptance 4 requires when a
# replacement isn't demonstrably stronger everywhere.
#
# Real-world wrinkle this repo actually has: a concurrent build harness and
# monitoring agent legitimately write to a short, FIXED list of paths
# during a live session, independent of whatever pytest happens to be
# running at the time. Those are not "pre-existing dirt" (a snapshot taken
# moments before this run may not have seen them yet) and they are not
# something this test suite could plausibly produce itself, so a diff
# against them is excluded by name below — never by exempting a whole
# directory the suite itself could write into (docs/, backend/, etc. are
# never excluded).
# --------------------------------------------------------------------------

_HARNESS_WRITTEN_PATHS = (
    # Appended by the PostToolUse monitor hook on EVERY tool call (see
    # .claude/hooks and the monitor script). A concurrent watchdog/build
    # session making its own tool calls while this suite runs appends to
    # this file mid-run; nothing inside this pytest process ever writes to
    # it, and no test in this suite exercises the monitor hook against the
    # real repo path (backend/tests/hooks/conftest.py's synthetic-project
    # fixtures redirect CLAUDE_PROJECT_DIR away from the real tree for
    # exactly this reason).
    ".claude/monitor/heartbeat.jsonl",
    # One JSON file per claimed ticket, written/removed by
    # `.claude/scripts/claim.sh` at claim/close/release — never through the
    # Edit/Write tool (see .claude/rules/harness-protocol.md). Not
    # gitignored (unlike .claude/evidence/, which is, and so never appears
    # in `git status --porcelain` output in the first place — it needs no
    # entry here). A concurrent session claiming, closing, or releasing a
    # DIFFERENT ticket while this suite runs changes this directory's
    # contents and thus its untracked status.
    ".claude/claims/",
    # Regenerated by `.claude/scripts/claim.sh` at every ticket
    # claim/close/release boundary (docs/TASKS.md is explicitly documented
    # as harness-generated, never hand-edited — see CLAUDE.md). A
    # concurrent session finishing a different ticket while this suite runs
    # rewrites it.
    "docs/TASKS.md",
)


def _excluded_path(path: str, excluded: str) -> bool:
    """True if ``path`` (as it appears in a porcelain line) is exactly, or —
    for a directory entry ending in "/" — nested under, ``excluded``."""
    if excluded.endswith("/"):
        return path == excluded or path.startswith(excluded)
    return path == excluded


def _is_harness_written_line(line: str) -> bool:
    # Porcelain v1: 2-char status code, 1 space, then the path (or, for a
    # rename/copy, "old -> new"). Treat the line as harness-owned only if
    # EVERY path it names is harness-owned — a rename that moves a
    # non-harness path in or out of a harness path is still a real event.
    entry = line[3:]
    paths = entry.split(" -> ") if " -> " in entry else [entry]
    return all(
        any(_excluded_path(path, excluded) for excluded in _HARNESS_WRITTEN_PATHS)
        for path in paths
    )


def _git_status_lines() -> list[str]:
    """Whole-tree ``git status --porcelain`` lines, minus lines that are
    entirely accounted for by ``_HARNESS_WRITTEN_PATHS``."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        line for line in result.stdout.splitlines() if line and not _is_harness_written_line(line)
    ]


_git_status_baseline: list[str] | None = None


# --------------------------------------------------------------------------
# Session start: capture the docs/eval-report baseline and the whole-tree
# git-status baseline, then reap any orphaned test_* schemas left behind by
# a pytest process that crashed before its own teardown fixture ran.
# --------------------------------------------------------------------------


def pytest_sessionstart(session: pytest.Session) -> None:
    global _docs_eval_report_baseline, _git_status_baseline
    _docs_eval_report_baseline = _docs_eval_report_fingerprint()
    _git_status_baseline = _git_status_lines()

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
# Session finish: drop this process's own schema (clean-exit cleanup), then
# assert docs/eval-report/ is exactly as dirty/clean as it was at the start
# of the run (content fingerprint), and separately assert the whole repo
# tree is exactly as dirty/clean as it was at the start of the run (git
# status, minus known harness-written paths). Both checks run regardless of
# each other's outcome — neither may mask the other — and either failing
# sets exitstatus = 1; a later success in this function must never reset it
# back to 0.
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

    if _docs_eval_report_baseline is not None:
        after = _docs_eval_report_fingerprint()
        if after != _docs_eval_report_baseline:
            session.exitstatus = 1
            print(
                "\nT-16 acceptance 2 VIOLATION: docs/eval-report/ content changed "
                f"during this run.\nbefore fingerprint: {_docs_eval_report_baseline!r}"
                f"\nafter fingerprint:  {after!r}"
            )

    if _git_status_baseline is not None:
        after_lines = _git_status_lines()
        before_set = set(_git_status_baseline)
        after_set = set(after_lines)
        added = sorted(after_set - before_set)
        removed = sorted(before_set - after_set)
        if added or removed:
            session.exitstatus = 1
            print(
                "\nT-23 acceptance 2 VIOLATION: repo tree not clean after this run "
                "(git status --porcelain changed, excluding known harness-written "
                f"paths — see _HARNESS_WRITTEN_PATHS).\nadded:   {added!r}"
                f"\nremoved: {removed!r}"
            )
