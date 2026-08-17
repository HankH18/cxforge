"""Differential proof that ``harness_lib.changed_since()`` answers the same
question it always did, WITHOUT ``git add -A``.

THE DEFECT THIS PINS. ``changed_since()`` opened with ``git add -A``, so merely
EVALUATING integrity mutated the repository: it staged the entire working tree.
Three consequences, each checked below:

  * a concurrent session's in-flight, unrelated files were dragged into the
    index by whichever session happened to close first;
  * ``python3 .claude/scripts/harness_lib.py integrity <tid> <commit>`` -- the
    read-only-looking CLI verb a watchdog or a human reaches for -- was a WRITE
    operation, so no observer could ask "is this tree in scope?" without
    changing the tree's answer to every other question;
  * it destroyed evidence it was meant to read: a staged change whose worktree
    copy had since been reverted got re-staged out of existence before the diff
    ran (``test_staged_then_reverted_*`` below).

THE REPLACEMENT reads the same three facts directly -- ``diff <commit>``
(commit -> worktree), ``diff --cached <commit>`` (commit -> index), and
``ls-files --others --exclude-standard`` (untracked, non-ignored) -- and unions
them. Every test here builds TWO byte-identical synthetic repos, applies one
scenario to each, runs the VERBATIM pre-fix algorithm (``_changed_since_v0``)
against one and the real, current ``harness_lib.changed_since`` against the
other, and compares. Equality is asserted where equality is the claim;
the three scenarios where the new answer is deliberately a STRICT SUPERSET each
get their own named test stating exactly what extra path is now caught and why
the old answer was a hole rather than a feature.

Nothing here touches the real repo: every scenario runs in ``tmp_path`` with
``CLAUDE_PROJECT_DIR`` pointed at it, and conftest's ``_assert_real_repo_untouched``
trip-wire is re-asserted after each subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import _assert_real_repo_untouched

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_LIB = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"

# One repo-mutating step: applied identically to both repos of a differential pair.
Scenario = Callable[[Path], None]

# Committed baseline every scenario starts from. Deterministic content, so the
# two repos a scenario is applied to are byte-identical before it runs.
BASE_FILES = {
    "tracked.txt": "tracked baseline\n",
    "todelete.txt": "doomed baseline\n",
    "torename.txt": "rename me\n" * 12,          # long enough to be a 100% rename match
    "staged.txt": "staged baseline\n",
    "sub/nested.txt": "nested baseline\n",
    ".claude/hooks/scope_guard.sh": "#!/usr/bin/env bash\nexit 0\n",  # a PROTECTED path to attack
    ".gitignore": "ignored/\n*.pyc\n",
    "docs/tickets.json": json.dumps(
        {
            "project": "changed-since-diff-test",
            "tickets": [
                {
                    "id": "T-1",
                    "title": "synthetic",
                    "objective": "exercise changed_since/integrity",
                    "acceptance": ["synthetic"],
                    "scope": ["tracked.txt"],
                    "depends_on": [],
                    "verify": "true && test -f tracked.txt",
                }
            ],
        }
    ),
}


# ---------------------------------------------------------------------------
# Synthetic project construction
# ---------------------------------------------------------------------------
def _git(proj: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=proj, capture_output=True, text=True, check=True,
    )


def _build_repo(root: Path) -> Path:
    """A disposable git repo carrying BASE_FILES, committed, plus a real copy
    of the harness scripts so ``import harness_lib`` inside it is the code
    under test rather than some other checkout's.
    """
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in BASE_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (root / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS_LIB, root / ".claude" / "scripts" / "harness_lib.py")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "changed-since-test@example.invalid")
    _git(root, "config", "user.name", "changed-since-test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _head(proj: Path) -> str:
    return _git(proj, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# The two implementations under comparison
# ---------------------------------------------------------------------------
def _changed_since_v0(proj: Path, commit: str) -> list[str]:
    """The PRE-FIX implementation, verbatim (harness_lib.py at 4618752 plus the
    pending patch's returncode check). Kept here as the differential reference:
    the whole point is that the new code must answer what THIS answered.
    """
    subprocess.run(["git", "add", "-A"], cwd=proj, capture_output=True)
    r = subprocess.run(
        ["git", "diff", "--name-only", commit], capture_output=True, text=True, cwd=proj,
    )
    s = subprocess.run(
        ["git", "diff", "--name-only", "--cached", commit],
        capture_output=True, text=True, cwd=proj,
    )
    return sorted(set(r.stdout.splitlines()) | set(s.stdout.splitlines()))


_CALL_CHANGED_SINCE = (
    "import json, sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "import harness_lib;"
    "print(json.dumps(harness_lib.changed_since(sys.argv[2])))"
)


def _changed_since_now(proj: Path, commit: str) -> list[str]:
    """The REAL, current ``harness_lib.changed_since`` -- run out of process
    with ``CLAUDE_PROJECT_DIR`` pointed at ``proj``, because harness_lib binds
    ``ROOT`` once at import time.
    """
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = subprocess.run(
        [sys.executable, "-c", _CALL_CHANGED_SINCE, str(proj / ".claude" / "scripts"), commit],
        capture_output=True, text=True, env=env, cwd=proj, timeout=30,
    )
    _assert_real_repo_untouched()
    assert r.returncode == 0, f"changed_since raised: {r.stderr}"
    return sorted(json.loads(r.stdout))


# ---------------------------------------------------------------------------
# Observable repo state (what "mutates the index" means, concretely)
# ---------------------------------------------------------------------------
def _index_state(proj: Path) -> str:
    """The index's staged content: mode, blob, stage, path for every entry.
    ``git add -A`` changes this; reading the tree must not."""
    return _git(proj, "--no-optional-locks", "ls-files", "--stage").stdout


def _porcelain(proj: Path) -> str:
    """Full status, staged column included: '??' (untracked) vs 'A ' (staged)
    is exactly the distinction ``git add -A`` erased."""
    return _git(proj, "--no-optional-locks", "status", "--porcelain").stdout


# ---------------------------------------------------------------------------
# Scenarios: each takes a freshly built repo at the baseline commit and makes
# ONE kind of change. Applied identically to both repos of a differential pair.
# ---------------------------------------------------------------------------
def _sc_untracked_new_file(proj: Path) -> None:
    (proj / "brand_new.txt").write_text("i did not exist at claim time\n")


def _sc_untracked_in_new_dir(proj: Path) -> None:
    (proj / "newdir").mkdir()
    (proj / "newdir" / "deep.txt").write_text("new dir, new file\n")


def _sc_modified_tracked(proj: Path) -> None:
    (proj / "tracked.txt").write_text("tracked MODIFIED out of band\n")


def _sc_staged_only(proj: Path) -> None:
    (proj / "staged.txt").write_text("staged MODIFIED\n")
    _git(proj, "add", "staged.txt")


def _sc_deleted(proj: Path) -> None:
    (proj / "todelete.txt").unlink()


def _sc_deleted_staged(proj: Path) -> None:
    _git(proj, "rm", "-q", "todelete.txt")


def _sc_renamed_git_mv(proj: Path) -> None:
    _git(proj, "mv", "torename.txt", "renamed.txt")


def _sc_renamed_plain_mv(proj: Path) -> None:
    shutil.move(str(proj / "torename.txt"), str(proj / "renamed.txt"))


def _sc_clean(proj: Path) -> None:
    pass


def _sc_ignored_file(proj: Path) -> None:
    (proj / "ignored").mkdir()
    (proj / "ignored" / "junk.txt").write_text("gitignored\n")
    (proj / "cache.pyc").write_text("gitignored too\n")


def _sc_out_of_band_bash_write_to_protected(proj: Path) -> None:
    """The compensating control integrity() exists FOR: a Bash-tool write to a
    PROTECTED path that the Edit/Write scope guard never saw."""
    subprocess.run(
        ["sh", "-c", "printf 'TAMPERED\\n' >> .claude/hooks/scope_guard.sh"],
        cwd=proj, check=True, capture_output=True,
    )


def _sc_staged_then_reverted(proj: Path) -> None:
    """Stage a change, then put the WORKTREE copy back to its committed bytes.
    The index still carries the change; the worktree no longer shows it."""
    (proj / "staged.txt").write_text("staged content that only the INDEX carries\n")
    _git(proj, "add", "staged.txt")
    (proj / "staged.txt").write_text(BASE_FILES["staged.txt"])


def _sc_non_ascii_untracked(proj: Path) -> None:
    (proj / "café.txt").write_text("non-ascii filename\n")


def _sc_everything_at_once(proj: Path) -> None:
    _sc_untracked_new_file(proj)
    _sc_modified_tracked(proj)
    _sc_staged_only(proj)
    _sc_deleted(proj)
    _sc_ignored_file(proj)
    _sc_out_of_band_bash_write_to_protected(proj)


def _differential(tmp_path: Path, scenario: Scenario) -> tuple[list[str], list[str], Path, Path]:
    """Apply ``scenario`` to two byte-identical repos; return (v0 answer,
    current answer, v0 repo, current repo) -- the repos come back so a test can
    also assert on what each implementation DID to them."""
    old_repo = _build_repo(tmp_path / "old")
    new_repo = _build_repo(tmp_path / "new")
    scenario(old_repo)
    scenario(new_repo)
    return (
        _changed_since_v0(old_repo, _head(old_repo)),
        _changed_since_now(new_repo, _head(new_repo)),
        old_repo,
        new_repo,
    )


# ---------------------------------------------------------------------------
# 1. The equivalence claim: identical answers, scenario by scenario.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "scenario", "expected"),
    [
        ("untracked new file", _sc_untracked_new_file, ["brand_new.txt"]),
        ("untracked file in a new dir", _sc_untracked_in_new_dir, ["newdir/deep.txt"]),
        ("modified tracked file", _sc_modified_tracked, ["tracked.txt"]),
        ("staged-only change", _sc_staged_only, ["staged.txt"]),
        ("deleted file", _sc_deleted, ["todelete.txt"]),
        ("staged deletion (git rm)", _sc_deleted_staged, ["todelete.txt"]),
        ("clean tree", _sc_clean, []),
        ("gitignored files", _sc_ignored_file, []),
        (
            "out-of-band Bash write to a PROTECTED path",
            _sc_out_of_band_bash_write_to_protected,
            [".claude/hooks/scope_guard.sh"],
        ),
        (
            "all of the above at once",
            _sc_everything_at_once,
            [
                ".claude/hooks/scope_guard.sh",
                "brand_new.txt",
                "staged.txt",
                "todelete.txt",
                "tracked.txt",
            ],
        ),
    ],
)
def test_answer_is_identical_to_the_add_dash_a_implementation(
    tmp_path: Path, name: str, scenario: Scenario, expected: list[str],
) -> None:
    """Same repo state in, same path list out -- and the expected list is
    spelled out literally rather than derived, so a mistake shared by BOTH
    implementations (e.g. both silently going blind) still fails."""
    old, new, _, _ = _differential(tmp_path, scenario)
    assert new == sorted(expected), f"{name}: new implementation answered {new}"
    assert new == old, (
        f"{name}: divergence from the add -A implementation.\n"
        f"  add -A said: {old}\n  new said:    {new}"
    )


# ---------------------------------------------------------------------------
# 2. Renames: the one place the new answer is deliberately STRICTER.
# ---------------------------------------------------------------------------
def test_git_mv_rename_now_reports_the_source_deletion_too(tmp_path: Path) -> None:
    """``git diff --name-only`` has rename detection ON by default, and prints
    only the DESTINATION for a detected rename. So under ``add -A`` a
    ``git mv <out-of-scope path> <in-scope path>`` reported only the in-scope
    destination: the deletion of the out-of-scope file was invisible and the
    close passed. That is a laundering route, not a feature, so the new code
    passes ``--no-renames`` and a rename faces the scope check as what it
    physically is -- a delete plus an add.
    """
    old, new, _, _ = _differential(tmp_path, _sc_renamed_git_mv)
    assert old == ["renamed.txt"], f"pre-fix behaviour changed underneath this test: {old}"
    assert new == ["renamed.txt", "torename.txt"]
    assert set(new) > set(old), "the new answer must be a strict superset, never a subset"


def test_plain_mv_and_git_mv_now_give_the_same_answer(tmp_path: Path) -> None:
    """Same physical change, two ways to make it. Under ``add -A`` the verdict
    depended on WHICH: a plain ``mv`` reported only the destination as well
    (add -A restaged it into a detectable rename). Both now report both paths,
    so an agent cannot pick the spelling that hides a deletion.
    """
    old_mv, new_mv, _, _ = _differential(tmp_path / "plain", _sc_renamed_plain_mv)
    _, new_gitmv, _, _ = _differential(tmp_path / "gitmv", _sc_renamed_git_mv)
    assert old_mv == ["renamed.txt"]
    assert new_mv == ["renamed.txt", "torename.txt"]
    assert new_mv == new_gitmv, "plain mv and git mv must yield the same integrity verdict"


# ---------------------------------------------------------------------------
# 3. Staged-then-reverted: the evidence `add -A` used to destroy.
# ---------------------------------------------------------------------------
def test_staged_then_reverted_change_is_now_caught(tmp_path: Path) -> None:
    """A change that lives ONLY in the index. ``add -A`` restaged the worktree
    over it before the diff ran, so the pre-fix code reported NOTHING -- it
    erased the very evidence it was called to read. Reading ``--cached``
    directly catches it.
    """
    old, new, _, _ = _differential(tmp_path, _sc_staged_then_reverted)
    assert old == [], f"pre-fix behaviour changed underneath this test: {old}"
    assert new == ["staged.txt"]


# ---------------------------------------------------------------------------
# 4. Path spelling: `-z` instead of core.quotePath mangling.
# ---------------------------------------------------------------------------
def test_non_ascii_path_is_reported_raw_not_quote_escaped(tmp_path: Path) -> None:
    """The file is caught by both implementations -- nothing goes unreported --
    but the pre-fix code spelled it the way ``core.quotePath`` writes it
    (``"caf\\303\\251.txt"``, octal-escaped and wrapped in literal quotes), and
    no scope glob can ever match that spelling, so an in-scope file with a
    non-ASCII name failed its own close. ``-z`` yields the real path.
    """
    old, new, _, _ = _differential(tmp_path, _sc_non_ascii_untracked)
    assert new == ["café.txt"]
    assert old == ['"caf\\303\\251.txt"'], f"pre-fix quoting changed underneath this test: {old}"
    assert len(old) == len(new), "same file count -- only the spelling differs"


# ---------------------------------------------------------------------------
# 5. The actual defect: evaluating integrity must not stage anything.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "scenario"),
    [
        ("untracked new file", _sc_untracked_new_file),
        ("modified tracked file", _sc_modified_tracked),
        ("deleted file", _sc_deleted),
        ("all at once", _sc_everything_at_once),
    ],
)
def test_changed_since_does_not_touch_the_index(
    tmp_path: Path, name: str, scenario: Scenario,
) -> None:
    """The whole point. Also asserts the pre-fix code DID mutate, so this test
    cannot pass by the scenario being too weak to stage anything."""
    proj = _build_repo(tmp_path / "new")
    scenario(proj)
    before_index, before_status = _index_state(proj), _porcelain(proj)
    _changed_since_now(proj, _head(proj))
    assert _index_state(proj) == before_index, f"{name}: changed_since staged something"
    assert _porcelain(proj) == before_status, f"{name}: changed_since changed working-tree status"

    old_proj = _build_repo(tmp_path / "old")
    scenario(old_proj)
    old_before = _porcelain(old_proj)
    _changed_since_v0(old_proj, _head(old_proj))
    assert _porcelain(old_proj) != old_before, (
        f"{name}: the pre-fix implementation did NOT mutate this repo, so this "
        f"scenario proves nothing about the fix"
    )


def test_a_concurrent_sessions_untracked_file_is_left_untracked(tmp_path: Path) -> None:
    """The concrete cross-session harm: session B's half-written file must
    still be untracked ('??') after session A evaluates integrity. Under
    ``add -A`` it came back staged ('A '), silently enrolled in A's next
    commit.
    """
    proj = _build_repo(tmp_path / "new")
    (proj / "session_b_wip.txt").write_text("another session's work in progress\n")
    _changed_since_now(proj, _head(proj))
    assert "?? session_b_wip.txt" in _porcelain(proj)

    old_proj = _build_repo(tmp_path / "old")
    (old_proj / "session_b_wip.txt").write_text("another session's work in progress\n")
    _changed_since_v0(old_proj, _head(old_proj))
    assert "A  session_b_wip.txt" in _porcelain(old_proj), (
        "pre-fix code no longer stages the file -- this regression test's premise is stale"
    )


def test_the_integrity_cli_verb_is_safe_for_an_observer_to_run(tmp_path: Path) -> None:
    """End to end through the CLI a watchdog or a human actually types:
    ``harness_lib.py integrity <tid> <commit>``. It must report the
    out-of-scope paths and leave the tree exactly as it found it.
    """
    proj = _build_repo(tmp_path / "new")
    _sc_everything_at_once(proj)
    commit = _head(proj)
    before_index, before_status = _index_state(proj), _porcelain(proj)

    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = subprocess.run(
        [sys.executable, str(proj / ".claude" / "scripts" / "harness_lib.py"),
         "integrity", "T-1", commit],
        capture_output=True, text=True, env=env, cwd=proj, timeout=30,
    )
    _assert_real_repo_untouched()

    assert r.returncode == 1, f"expected an integrity failure, got rc={r.returncode}: {r.stderr}"
    reported = sorted(x for x in r.stdout.splitlines() if x)
    # T-1's scope is ["tracked.txt"], so tracked.txt is the one legitimate change.
    assert reported == [".claude/hooks/scope_guard.sh", "brand_new.txt", "staged.txt",
                        "todelete.txt"]
    assert _index_state(proj) == before_index, "the integrity VERB staged something"
    assert _porcelain(proj) == before_status, "the integrity VERB changed working-tree status"


# ---------------------------------------------------------------------------
# 6. The pending patch's unanswerable-diff refusal must survive the rewrite.
# ---------------------------------------------------------------------------
def test_unresolvable_commit_still_raises_rather_than_answering_empty(tmp_path: Path) -> None:
    """An empty diff and an unanswerable diff are opposite facts. The rewrite
    must keep raising IntegrityUnavailable -- returning [] here would let a
    close pass vacuously with scope enforcement silently off.
    """
    proj = _build_repo(tmp_path / "new")
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    r = subprocess.run(
        [sys.executable, "-c", _CALL_CHANGED_SINCE,
         str(proj / ".claude" / "scripts"), "0" * 40],
        capture_output=True, text=True, env=env, cwd=proj, timeout=30,
    )
    _assert_real_repo_untouched()
    assert r.returncode != 0, f"expected a raise, got stdout={r.stdout!r}"
    assert "IntegrityUnavailable" in r.stderr
    assert "0000000000000000000000000000000000000000" in r.stderr, (
        "the refusal must name the start_commit it could not resolve"
    )
