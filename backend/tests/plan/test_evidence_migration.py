"""T-31 acceptance 4: "Regression tests prove both a legacy closure record
and a new fingerprint-bound JSON receipt behave as specified."

New under T-31 (the harness-sync migration). Nothing here supersedes an
existing test -- this module exists because nothing exercised the v2
lifecycle's two evidence formats side by side before.

Every test below builds its OWN disposable project in `tmp_path`: `git
init`, an initial commit, a synthetic `docs/tickets.json`, and a copy of
the REAL `.claude/scripts/` and `.claude/hooks/` (never the real files
invoked in place -- every subprocess call passes `CLAUDE_PROJECT_DIR`
pointed at the copy, and the script binary invoked is the COPY's path, so
even a bug that ignored that env var could not reach the real repo). This
session holds a live claim on T-31 in the real repo; none of these tests
ever touch the real `.claude/claims/`, `.claude/evidence/`, or git history.

What each proves, mapped to T-31 acceptance 2's migration policy
(`.claude/evidence-v1/README.md`, also pinned by test 6 below):

1. `test_v1_pass_file_is_inert` -- a `.claude/evidence-v1/<id>.pass` bare
   epoch is INERT: the ticket still derives `queue`, is still claimable,
   and `harness_lib.receipt()` returns `None` for it.
2. `test_v2_receipt_is_honoured_and_gates_dependents` -- a
   `.claude/evidence/<id>.json` receipt IS honoured: the ticket derives
   `resolved`, a claim on it is refused, and a ticket that `depends_on` it
   goes from unclaimable to claimable the moment the receipt exists.
3. `test_receipt_fingerprint_is_content_bound_and_recomputable` -- editing
   a scope file's bytes changes what `harness_lib.py fingerprint <tid>`
   recomputes, and the value stored in the receipt at close time equals
   what a live recompute produced from that same content.
4. `test_receipt_commit_matches_head_at_close_time` -- the receipt's
   `commit` field equals `git rev-parse HEAD` in the ticket's own project,
   read immediately after `close`.
5. `test_no_harness_source_reads_evidence_v1` -- greps the REAL
   `.claude/scripts/**` and `.claude/hooks/**` sources for any reference to
   `evidence-v1`, and asserts there is none: no code path anywhere upgrades
   or reads a `.pass` file as evidence.
6. `test_evidence_v1_readme_exists_and_states_the_inert_policy` -- the
   real `.claude/evidence-v1/README.md` exists, states the inert policy in
   the terms T-31's non-goals require ("no fabrication"), and names every
   ticket id that actually has a `.pass` file today (computed from the
   real directory, not hand-copied, so it can't silently drift).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
REAL_EVIDENCE_V1_DIR = REPO_ROOT / ".claude" / "evidence-v1"

# Guardrails: this module must never touch the real repo's live lifecycle
# state, no matter what a test does wrong.
REAL_CLAIMS_DIR = REPO_ROOT / ".claude" / "claims"
REAL_EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"


# --------------------------------------------------------------------------
# synthetic-project construction
# --------------------------------------------------------------------------


def _ticket(tid: str, scope: list[str], depends_on: list[str] | None = None) -> dict:
    return {
        "id": tid,
        "title": f"synthetic ticket {tid}",
        "objective": "T-31 evidence-migration test fixture",
        "acceptance": ["n/a"],
        "verify": "true",  # trivial, always-passing, LINT_RULES-clean verify
        "scope": scope,
        "depends_on": depends_on or [],
        "non_goals": [],
        "parallel_safe": False,
        "status": "open",
    }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )


def _build_project(
    tmp_path: Path, tickets: list[dict], scope_file_contents: dict[str, str]
) -> Path:
    """A disposable git repo + harness install: real `.claude/scripts/` and
    `.claude/hooks/` copied verbatim, a synthetic `docs/tickets.json`, the
    ticket scope files seeded with the given content, all in one initial
    commit."""
    root = tmp_path
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "tickets.json").write_text(json.dumps({"tickets": tickets}, indent=2))

    shutil.copytree(
        REAL_SCRIPTS_DIR, root / ".claude" / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        REAL_HOOKS_DIR, root / ".claude" / "hooks", ignore=shutil.ignore_patterns("__pycache__")
    )

    for rel, content in scope_file_contents.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    assert _git(["init", "-q"], root).returncode == 0
    _git(["config", "user.email", "evidence-migration-test@example.com"], root)
    _git(["config", "user.name", "Evidence Migration Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)  # hermetic: no host gpg dependency
    _git(["add", "-A"], root)
    r = _git(["commit", "-q", "-m", "initial"], root)
    assert r.returncode == 0, r.stderr

    for guard_dir in (REAL_CLAIMS_DIR, REAL_EVIDENCE_DIR):
        assert guard_dir != root, "test must never alias the real repo's lifecycle state"

    return root


def _run_harness(
    root: Path, args: list[str], *, session: str | None = "sess-test"
) -> subprocess.CompletedProcess[str]:
    """Invoke the harness_lib.py COPY inside `root` (never the real repo's
    copy), with CLAUDE_PROJECT_DIR pinned to `root`."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    if session is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return subprocess.run(
        [sys.executable, str(root / ".claude" / "scripts" / "harness_lib.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=30,
    )


def _status_of(root: Path, tid: str) -> str:
    result = _run_harness(root, ["status_board"], session=None)
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == tid:
            return parts[1]
    raise AssertionError(f"{tid} not in status_board output: {result.stdout!r}")


def _receipt_via_function_call(root: Path, tid: str) -> dict | None:
    """Calls the real `harness_lib.receipt(tid)` function directly (not the
    CLI), in its own subprocess against the project copy, and returns its
    JSON-serialised result."""
    code = (
        "import sys, json; "
        f"sys.path.insert(0, {str(root / '.claude' / 'scripts')!r}); "
        "import harness_lib; "
        f"print(json.dumps(harness_lib.receipt({tid!r})))"
    )
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=root, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _fingerprint_cli(root: Path, tid: str) -> str:
    result = _run_harness(root, ["fingerprint", tid], session=None)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --------------------------------------------------------------------------
# 1. v1 .pass evidence is inert
# --------------------------------------------------------------------------


def test_v1_pass_file_is_inert(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path,
        [_ticket("T-LEGACY", ["src/legacy_feature.py"])],
        {"src/legacy_feature.py": "legacy content\n"},
    )
    v1_dir = root / ".claude" / "evidence-v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    (v1_dir / "T-LEGACY.pass").write_text(str(int(time.time())))

    # 1a. still derives "queue", not "resolved"
    assert _status_of(root, "T-LEGACY") == "queue"

    # 1b. harness_lib.receipt() returns None for it
    assert _receipt_via_function_call(root, "T-LEGACY") is None

    # 1c. still claimable
    claim = _run_harness(root, ["claim", "T-LEGACY", "proving v1 evidence is inert"])
    assert claim.returncode == 0, (
        f"T-LEGACY should still be claimable: {claim.stdout} {claim.stderr}"
    )
    assert "claimed T-LEGACY" in claim.stdout
    assert _status_of(root, "T-LEGACY") == "in_progress"


# --------------------------------------------------------------------------
# 2. v2 JSON receipt is honoured and gates dependents
# --------------------------------------------------------------------------


def test_v2_receipt_is_honoured_and_gates_dependents(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path,
        [
            _ticket("T-NEW", ["src/new_feature.py"]),
            _ticket("T-DEP", ["src/dep_feature.py"], depends_on=["T-NEW"]),
        ],
        {"src/new_feature.py": "v1\n", "src/dep_feature.py": "dep content\n"},
    )

    # T-DEP is NOT claimable before T-NEW has a receipt.
    blocked = _run_harness(root, ["claim", "T-DEP", "should be blocked"], session="sess-dep")
    assert blocked.returncode == 1
    assert "dependency T-NEW has no receipt" in blocked.stdout

    # Real lifecycle: claim T-NEW, then close it -> mints .claude/evidence/T-NEW.json
    claim = _run_harness(root, ["claim", "T-NEW", "do the work"], session="sess-new")
    assert claim.returncode == 0, claim.stdout + claim.stderr
    close = _run_harness(root, ["close"], session="sess-new")
    assert close.returncode == 0, close.stdout + close.stderr
    assert (root / ".claude" / "evidence" / "T-NEW.json").exists()

    # Derives "resolved"
    assert _status_of(root, "T-NEW") == "resolved"

    # A claim on T-NEW is now refused
    reclaim = _run_harness(root, ["claim", "T-NEW", "should be refused"], session="sess-other")
    assert reclaim.returncode == 1
    assert "already has a receipt" in reclaim.stdout

    # T-DEP is now claimable
    dep_claim = _run_harness(
        root, ["claim", "T-DEP", "unblocked by T-NEW's receipt"], session="sess-dep"
    )
    assert dep_claim.returncode == 0, dep_claim.stdout + dep_claim.stderr
    assert _status_of(root, "T-DEP") == "in_progress"


# --------------------------------------------------------------------------
# 3. fingerprint is content-bound and CLI-recomputable
# --------------------------------------------------------------------------


def test_receipt_fingerprint_is_content_bound_and_recomputable(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path,
        [_ticket("T-FP", ["src/fp_feature.py"])],
        {"src/fp_feature.py": "v1\n"},
    )

    claim = _run_harness(root, ["claim", "T-FP", "work"], session="sess-fp")
    assert claim.returncode == 0, claim.stdout + claim.stderr

    # Simulate the ticket's real work: edit the scope file's content.
    (root / "src" / "fp_feature.py").write_text("v1\nv2\n")
    fp_before_close = _fingerprint_cli(root, "T-FP")

    close = _run_harness(root, ["close"], session="sess-fp")
    assert close.returncode == 0, close.stdout + close.stderr
    receipt = json.loads((root / ".claude" / "evidence" / "T-FP.json").read_text())

    # The stored receipt fingerprint equals what a live recompute of the
    # edited content produced right before close.
    assert receipt["fingerprint"] == fp_before_close

    # Editing the content AGAIN (after close) changes what a fresh CLI
    # recompute produces, while the already-minted receipt's stored value
    # (an immutable record of what was verified) does not move.
    (root / "src" / "fp_feature.py").write_text("v1\nv2\nv3\n")
    fp_after_further_edit = _fingerprint_cli(root, "T-FP")
    assert fp_after_further_edit != fp_before_close
    stored_after_edit = json.loads((root / ".claude" / "evidence" / "T-FP.json").read_text())
    assert stored_after_edit["fingerprint"] == receipt["fingerprint"]


# --------------------------------------------------------------------------
# 4. commit matches HEAD at close time
# --------------------------------------------------------------------------


def test_receipt_commit_matches_head_at_close_time(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path,
        [_ticket("T-COMMIT", ["src/commit_feature.py"])],
        {"src/commit_feature.py": "content\n"},
    )
    claim = _run_harness(root, ["claim", "T-COMMIT", "work"], session="sess-commit")
    assert claim.returncode == 0, claim.stdout + claim.stderr

    close = _run_harness(root, ["close"], session="sess-commit")
    assert close.returncode == 0, close.stdout + close.stderr

    head = _git(["rev-parse", "HEAD"], root).stdout.strip()
    receipt = json.loads((root / ".claude" / "evidence" / "T-COMMIT.json").read_text())
    assert receipt["commit"] == head


# --------------------------------------------------------------------------
# 5. no code path anywhere upgrades a .pass file into a JSON receipt
# --------------------------------------------------------------------------


def test_no_harness_source_reads_evidence_v1() -> None:
    """Every .py/.sh under the REAL .claude/scripts/ and .claude/hooks/ --
    the harness's entire source -- is scanned for any reference to
    "evidence-v1" (or "evidence_v1"). None should exist: the only sanctioned
    reader of .claude/evidence-v1/ is a human/auditor, never harness code.
    This is what makes .claude/evidence-v1/README.md's "never upgraded"
    claim actually true rather than aspirational.
    """
    offenders: list[str] = []
    for directory in (REAL_SCRIPTS_DIR, REAL_HOOKS_DIR):
        for path in directory.rglob("*"):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            if path.suffix not in (".py", ".sh"):
                continue
            text = path.read_text(errors="replace")
            if "evidence-v1" in text or "evidence_v1" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "harness source(s) reference evidence-v1 -- a .pass file must never "
        f"be read/upgraded by harness code: {offenders}"
    )


# --------------------------------------------------------------------------
# 6. the migration-policy doc exists and can't silently disappear or drift
# --------------------------------------------------------------------------


def test_evidence_v1_readme_exists_and_states_the_inert_policy() -> None:
    readme = REAL_EVIDENCE_V1_DIR / "README.md"
    assert readme.exists(), (
        ".claude/evidence-v1/README.md is missing -- the migration policy "
        "must stay documented, not just remembered"
    )
    text = readme.read_text()
    lower = text.lower()

    assert "inert" in lower, "README must state the v1 records are inert"
    assert "fabricat" in lower, (
        "README must forbid fabricating commit/fingerprint metadata for v1 "
        "records (T-31 non_goals)"
    )
    assert ".claude/evidence/" in text, (
        "README must name the real v2 receipt location the lifecycle "
        "actually reads"
    )

    real_ids = sorted(
        (p.stem for p in REAL_EVIDENCE_V1_DIR.glob("*.pass")),
        key=lambda t: int(t.split("-")[1]),
    )
    assert real_ids, "expected at least one real .pass file to check the README against"
    for tid in real_ids:
        assert tid in text, (
            f"{tid} has a .claude/evidence-v1/{tid}.pass file but is not "
            f"listed in evidence-v1/README.md"
        )
