"""What a ticket-start commit may absorb, and what a claim record may assert.

Two independent laundering routes converge on the same outcome -- a fingerprint-bound
receipt certifying a scope that never covered the change -- and this file binds the
closure of both.

**Route 1: paths the harness could not see.** `cmd_claim` sweeps the whole tree into its
own `ticket-start:` commit (`git add -A`) and `integrity()` only diffs FORWARD from that
commit, so anything the pre-claim dirty check misses is structurally invisible at close.
The dirty check reads `git status --porcelain`; the close-time check reads
`git diff --name-only`. BOTH used to let a rename through, because porcelain v1's default
text format reports a rename as one record naming two paths (`R  <old> -> <new>`) and
`git diff` with rename detection on prints only the destination. A reader that keeps the
destination stops seeing the source: `git mv docs/SPEC.md src/SPEC.md` under a `src/**`
scope reported only `src/SPEC.md`, which is in scope, so the protected plan file's
DELETION passed both gates. Worse, the old `" -> "` split was not gated on the status
code at all, so an ordinary `M` record whose path merely CONTAINS that substring was
rewritten into its own in-scope-looking suffix. `--no-renames` (both readers) plus `-z`
(no C-quoting to unpick) removes the ambiguity at the source rather than parsing around
it. A `git status` that FAILS is likewise not a clean tree -- it is an unanswered
question, and answering it "clean" is the same vacuous pass `IntegrityUnavailable` exists
to prevent.

**Route 2: records that do not say who owns them.** `.claude/claims/<session_id>.json` is
written only by `cmd_claim`, only for the session whose id names it, and every reader
(close, release, the scope guard, the stop guard) resolves ownership from that path
alone -- THE FILENAME IS THE ATTRIBUTION. A record whose own `session` field disagrees
with its filename therefore asserts two contradictory owners with nothing to adjudicate
between them. `test_close_unattributed_claim_gap.py` covers `cmd_close`'s half of this
per-case. What this file adds is everything that reads the same record and used to crash
or silently comply:

  * `release` -- MUST still work on an incoherent record. An agent may not hand-edit
    `.claude/claims/**` (`harness_lib.ABSOLUTE` denies it unconditionally) and `close`
    now refuses the record, so release is the only sanctioned exit; when it crashed too,
    a corrupt record wedged the session with no protocol-legal way out. Release certifies
    nothing -- it writes no evidence -- so degrading here cannot launder anything.
  * `claim` -- refuses by name instead of a `KeyError` traceback out of
    `session_claim(sid)['ticket']`.
  * `status_board` -- reports the broken record instead of dying on it, so one wedged
    session is not a repo-wide outage.
  * the stop guard -- an incoherent record is still an OPEN one; stopping silently on it
    is how a misattributed claim outlives the session that made it.
  * the scope guard -- must not let such a record UNLOCK anything, while leaving
    `.claude/NEEDS_HUMAN.md` writable (META_ALLOW is checked first) so the session can
    report the problem.

Self-contained like `test_verify_gate.py` and `test_close_unattributed_claim_gap.py`:
every test builds its own synthetic git project in `tmp_path` (real git repo,
hand-authored `docs/tickets.json`, copies of the real `.claude/scripts/` and
`.claude/hooks/`) and never touches the real repo's `.claude/claims/`,
`.claude/evidence/`, `docs/`, or git history -- other sessions may hold live claims there
while this suite runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"

LIVE_CLAIMS_DIR = REPO_ROOT / ".claude" / "claims"
LIVE_EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"

# Well outside the real plan's range (T-0..T-31) and distinct from the ids the
# neighbouring self-contained files use, so a leak into the real repo is unmistakable.
TID = "T-9300"


# ---------------------------------------------------------------------------
# Hermeticity trip-wire
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _never_touch_the_real_repos_live_harness_state() -> Iterator[None]:
    """Other sessions may be claiming/closing real tickets while this suite runs, so this
    cannot snapshot-and-diff the whole directory. It asserts instead that this file's
    synthetic ticket id never appears as a real claim or receipt -- the one leak signature
    proving a subprocess escaped its `tmp_path` project.
    """

    def leaked() -> list[str]:
        hits = []
        if LIVE_EVIDENCE_DIR.is_dir() and (LIVE_EVIDENCE_DIR / f"{TID}.json").exists():
            hits.append(f".claude/evidence/{TID}.json")
        if LIVE_CLAIMS_DIR.is_dir():
            for p in LIVE_CLAIMS_DIR.glob("*.json"):
                try:
                    rec = json.loads(p.read_text())
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("ticket") == TID:
                    hits.append(f".claude/claims/{p.name}")
        return hits

    assert not leaked(), "the real repo already carries this file's synthetic ticket id"
    yield
    assert not leaked(), (
        "a subprocess in this file mutated the REAL repo's harness state -- "
        "CLAUDE_PROJECT_DIR must have leaked out of tmp_path"
    )


# ---------------------------------------------------------------------------
# Synthetic project
# ---------------------------------------------------------------------------
TICKETS_DOC: dict[str, Any] = {
    "tickets": [
        {
            "id": TID,
            "title": "synthetic in-scope work",
            "objective": "exercise the real lifecycle against a disposable project",
            "refs": [],
            "acceptance": ["src/feature.txt exists"],
            "verify": "test -f src/feature.txt",
            "scope": ["src/**"],
            "depends_on": [],
            "non_goals": [],
            "parallel_safe": False,
            "priority": "next",
            "status": "open",
        }
    ]
}


def _git(proj: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True)
    if check:
        assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "src").mkdir(parents=True)
    (proj / "docs" / "tickets.json").write_text(json.dumps(TICKETS_DOC))
    # An out-of-scope, PROTECTED-class plan file for the rename cases to move.
    (proj / "docs" / "SPEC.md").write_text("# spec\nthe approved plan intent\n")
    (proj / "src" / "keep.txt").write_text("kept\n")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REAL_SCRIPTS_DIR, proj / ".claude" / "scripts", ignore=ignore)
    shutil.copytree(REAL_HOOKS_DIR, proj / ".claude" / "hooks", ignore=ignore)
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "hooktest@example.com")
    _git(proj, "config", "user.name", "hook-test")
    _git(proj, "config", "commit.gpgsign", "false")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "initial")
    return proj


def _env(proj: Path, session_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _claim_sh(proj: Path, sid: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), *args],
        cwd=proj, capture_output=True, text=True, env=_env(proj, sid), timeout=60,
    )


def _lib(
    proj: Path, sid: str, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Drive harness_lib.py directly -- which is exactly what the `.claude/hooks/*.sh`
    shims exec -- so the hook CONTRACT is bound rather than the shell indirection."""
    return subprocess.run(
        [sys.executable, str(proj / ".claude" / "scripts" / "harness_lib.py"), *args],
        cwd=proj, capture_output=True, text=True, env=_env(proj, sid),
        input=stdin, timeout=60,
    )


def _claim_path(proj: Path, sid: str) -> Path:
    return proj / ".claude" / "claims" / f"{sid}.json"


def _evidence_path(proj: Path) -> Path:
    return proj / ".claude" / "evidence" / f"{TID}.json"


def _corrupt(proj: Path, sid: str, mutate: Any) -> None:
    """Claim for real, then break the record -- so every case tests something the real
    lifecycle produced, not a hand-forged fixture that never went through it."""
    claimed = _claim_sh(proj, sid, "claim", TID, "note")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    path = _claim_path(proj, sid)
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record))


def _no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback (most recent call last)" not in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Route 1a: a rename must not smuggle an out-of-scope path past the claim gate
# ---------------------------------------------------------------------------
def test_claim_refuses_a_pre_claim_rename_that_moves_an_out_of_scope_file_into_scope(
    tmp_path: Path,
) -> None:
    """`git mv docs/SPEC.md src/SPEC.md` before claiming a `src/**` ticket.

    Porcelain v1 reports this as the single record `R  docs/SPEC.md -> src/SPEC.md`, and
    the old reader kept only the destination -- which matches `src/**`, so the gate saw a
    clean tree, the ticket-start commit absorbed the deletion of a protected plan file,
    and the close minted a receipt. The A/B control is
    `test_claim_still_refuses_a_plain_out_of_scope_deletion` below: the same file removed
    with `git rm` was ALWAYS refused, so the rename parse was precisely what defeated the
    gate.
    """
    proj = _make_project(tmp_path)
    _git(proj, "mv", "docs/SPEC.md", "src/SPEC.md")
    # Precondition: git really does pair this as one rename record (if it stops doing so,
    # this test would pass for the wrong reason).
    porcelain = _git(proj, "status", "--porcelain").stdout
    assert "docs/SPEC.md -> src/SPEC.md" in porcelain, porcelain

    result = _claim_sh(proj, "renamer", "claim", TID, "note")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSING to claim" in result.stdout
    assert "docs/SPEC.md" in result.stdout
    # Nothing may have happened: no start commit, no claim record.
    assert not _claim_path(proj, "renamer").exists()
    assert _git(proj, "log", "--oneline").stdout.count("\n") == 1


def test_claim_still_refuses_a_plain_out_of_scope_deletion(tmp_path: Path) -> None:
    """A/B control for the test above: the same protected file, deleted rather than
    renamed, was refused even before `--no-renames`. Keeping both proves the fix narrowed
    a specific blind spot rather than that the gate is simply refusing everything."""
    proj = _make_project(tmp_path)
    _git(proj, "rm", "-q", "docs/SPEC.md")

    result = _claim_sh(proj, "deleter", "claim", TID, "note")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "docs/SPEC.md" in result.stdout


def test_claim_refuses_a_modified_path_whose_name_contains_the_rename_arrow(
    tmp_path: Path,
) -> None:
    """The old `" -> "` split was applied to every record regardless of status code, so a
    plain `M` on a path literally containing that substring was rewritten to its own
    suffix. A tracked out-of-scope file at `docs/SPEC.md -> src/ok.txt` therefore read as
    `src/ok.txt` -- in scope -- and its modification was absorbed. `-z` also removes the
    C-quoting the old `.strip('"')` was papering over, which is what silently repaired
    the mangled suffix into a plausible path."""
    proj = _make_project(tmp_path)
    weird_dir = proj / "docs" / "SPEC.md -> src"
    weird_dir.mkdir()
    (weird_dir / "ok.txt").write_text("v1\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "add a path containing the arrow")
    (weird_dir / "ok.txt").write_text("v2 out-of-scope modification\n")

    result = _claim_sh(proj, "arrow", "claim", TID, "note")

    assert result.returncode == 1, result.stdout + result.stderr
    # The TRUE path, not the `src/ok.txt` suffix the old parse invented.
    assert "docs/SPEC.md -> src/ok.txt" in result.stdout
    assert not _claim_path(proj, "arrow").exists()


def test_claim_refuses_when_git_status_cannot_answer_at_all(tmp_path: Path) -> None:
    """An unanswerable `git status` is not a clean tree. The reader used to take
    `.stdout` regardless of return code, so a broken GIT_DIR produced an empty list, the
    gate passed vacuously, and `cmd_claim` went on to write a record with
    `start_commit: ""` -- a claim whose close could never be integrity-checked and which
    could only ever be released. This is the same conflation `IntegrityUnavailable`'s own
    docstring condemns, twelve lines above where it was happening."""
    proj = _make_project(tmp_path)
    shutil.rmtree(proj / ".git")

    result = _claim_sh(proj, "nogit", "claim", TID, "note")

    assert result.returncode != 0, result.stdout + result.stderr
    _no_traceback(result)
    assert "could not report the working tree state" in result.stdout
    assert not _claim_path(proj, "nogit").exists()


# ---------------------------------------------------------------------------
# Route 1b: a rename must not smuggle an out-of-scope path past the CLOSE gate
# ---------------------------------------------------------------------------
def test_close_integrity_sees_both_sides_of_a_rename_made_during_the_ticket(
    tmp_path: Path,
) -> None:
    """The claim-time gate is only half the route. `changed_since()` used to run
    `git diff --name-only <start>` with rename detection ON, which prints only the
    destination -- so a session that claimed a clean tree and THEN moved a protected plan
    file out of the way closed with zero integrity findings and a minted receipt. This is
    the more serious half: it needs no pre-claim setup and no release/re-claim loop.
    """
    proj = _make_project(tmp_path)
    claimed = _claim_sh(proj, "mover", "claim", TID, "note")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr

    _git(proj, "mv", "docs/SPEC.md", "src/SPEC.md")
    (proj / "src" / "feature.txt").write_text("the legitimate in-scope work\n")

    result = _claim_sh(proj, "mover", "close")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "INTEGRITY FAIL" in result.stdout
    assert "docs/SPEC.md" in result.stdout
    assert not _evidence_path(proj).exists()


def test_an_honest_in_scope_rename_still_closes(tmp_path: Path) -> None:
    """Positive control: `--no-renames` makes the check see BOTH paths, so a rename
    entirely within scope must still pass. Without this, "refuses renames" would be
    indistinguishable from "refuses everything"."""
    proj = _make_project(tmp_path)
    claimed = _claim_sh(proj, "honest", "claim", TID, "note")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr

    _git(proj, "mv", "src/keep.txt", "src/renamed.txt")
    (proj / "src" / "feature.txt").write_text("the legitimate in-scope work\n")

    result = _claim_sh(proj, "honest", "close")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "closed" in result.stdout
    assert _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Route 2: every reader of an incoherent claim record
# ---------------------------------------------------------------------------
def test_release_still_retires_an_unreadable_record_so_the_session_is_not_wedged(
    tmp_path: Path,
) -> None:
    """The one command that must DEGRADE rather than refuse. `close` now refuses an
    incoherent record and `.claude/claims/**` is unconditionally Edit/Write-denied
    (`harness_lib.ABSOLUTE`), so if `release` also crashed -- and it did, with a raw
    traceback out of `json.load` -- the only remaining remedy was `rm` via Bash, the exact
    thing the harness exists to prevent. Release writes no evidence, so degrading here
    certifies nothing; it retires the record and tells the human."""
    proj = _make_project(tmp_path)
    claimed = _claim_sh(proj, "wedged", "claim", TID, "note")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    _claim_path(proj, "wedged").write_text("{not valid json")

    result = _claim_sh(proj, "wedged", "release", "record went bad")

    assert result.returncode == 0, result.stdout + result.stderr
    _no_traceback(result)
    assert "released" in result.stdout
    assert "INCOHERENT CLAIM RECORD" in result.stdout
    assert not _claim_path(proj, "wedged").exists()
    # The human channel carries the detail, per harness-protocol rule 7.
    needs_human = (proj / ".claude" / "NEEDS_HUMAN.md").read_text()
    assert "INCOHERENT CLAIM RECORD" in needs_human
    assert "record went bad" in needs_human
    # Release certifies nothing.
    assert not _evidence_path(proj).exists()


def test_release_of_a_healthy_claim_is_unchanged(tmp_path: Path) -> None:
    """Positive control for the degradation above: the normal path still names the ticket
    and logs no incoherence marker."""
    proj = _make_project(tmp_path)
    claimed = _claim_sh(proj, "healthy", "claim", TID, "note")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr

    result = _claim_sh(proj, "healthy", "release", "changed my mind")

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"released {TID}" in result.stdout
    assert "INCOHERENT" not in result.stdout
    assert not _claim_path(proj, "healthy").exists()


def test_claim_refuses_by_name_instead_of_crashing_on_this_sessions_broken_record(
    tmp_path: Path,
) -> None:
    """`cmd_claim`'s "you already own something" branch did
    `session_claim(sid)['ticket']` with no guard, so the session that most needed to be
    told what was wrong got a `KeyError`/`JSONDecodeError` traceback instead."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "breaker", lambda rec: rec.pop("ticket"))

    result = _claim_sh(proj, "breaker", "claim", TID, "note")

    assert result.returncode == 1, result.stdout + result.stderr
    _no_traceback(result)
    assert ".claude/claims/breaker.json" in result.stdout
    assert "missing required field" in result.stdout


def test_status_board_reports_a_broken_record_instead_of_dying_on_it(
    tmp_path: Path,
) -> None:
    """`claims()` used to `json.load` every file with no guard, so one corrupt record
    took down every reader that merely wanted to know which tickets are held -- turning a
    single broken session into a repo-wide outage. The board now still renders, and names
    the offending record rather than hiding it."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "boardbreaker", lambda rec: rec.__setitem__("session", "impostor"))

    result = _lib(proj, "observer", "status_board")

    assert result.returncode == 0, result.stdout + result.stderr
    _no_traceback(result)
    assert TID in result.stdout
    assert "INCOHERENT" in result.stdout
    assert ".claude/claims/boardbreaker.json" in result.stdout


def test_stop_guard_blocks_on_a_misattributed_record_instead_of_letting_it_outlive_the_session(
    tmp_path: Path,
) -> None:
    """A misattributed record is still an OPEN claim. The stop guard used to read it
    straight through `session_claim` and either block with the impostor's own ticket (as
    though nothing were wrong) or, for unparseable JSON, crash -- and a crashed Stop hook
    is a Stop hook that blocks nothing at all."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "stopper", lambda rec: rec.__setitem__("session", "impostor"))

    result = _lib(proj, "stopper", "hook-stop",
                  stdin=json.dumps({"session_id": "stopper"}))

    assert result.returncode == 0, result.stdout + result.stderr
    _no_traceback(result)
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert ".claude/claims/stopper.json" in payload["reason"]
    assert "impostor" in payload["reason"]


def test_stop_guard_is_silent_when_no_claim_exists_at_all(tmp_path: Path) -> None:
    """Absence is not incoherence: a session holding nothing must still be free to stop.
    Guards against the refusal above widening into "block every stop"."""
    proj = _make_project(tmp_path)

    result = _lib(proj, "nobody", "hook-stop",
                  stdin=json.dumps({"session_id": "nobody"}))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""


def test_scope_guard_fails_closed_while_a_misattributed_record_exists(
    tmp_path: Path,
) -> None:
    """An incoherent record must UNLOCK nothing. Its scope would come from a ticket it
    cannot be trusted to name, so it is no licence to write a protected plan/harness file
    -- and it is no coherent constraint on an ordinary one either. Both fail closed."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "guarded", lambda rec: rec.__setitem__("session", "impostor"))

    for target in ("docs/tickets.json", "src/feature.txt"):
        result = _lib(proj, "guarded", "hook-scope", stdin=json.dumps(
            {"session_id": "guarded", "tool_input": {"file_path": str(proj / target)}}))
        assert result.returncode == 0, result.stdout + result.stderr
        _no_traceback(result)
        payload = json.loads(result.stdout)
        decision = payload["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", target
        assert ".claude/claims/guarded.json" in decision["permissionDecisionReason"]


def test_scope_guard_still_allows_the_human_channel_while_a_record_is_incoherent(
    tmp_path: Path,
) -> None:
    """META_ALLOW is checked BEFORE the coherence check for a reason: harness-protocol
    rule 7 requires the session to write `.claude/NEEDS_HUMAN.md`, and the moment a claim
    record goes bad is precisely when it has something to report. A fail-closed guard that
    also gagged the report would leave nothing to do but stop silently."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "reporter", lambda rec: rec.__setitem__("session", "impostor"))

    result = _lib(proj, "reporter", "hook-scope", stdin=json.dumps(
        {"session_id": "reporter",
         "tool_input": {"file_path": str(proj / ".claude" / "NEEDS_HUMAN.md")}}))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""       # empty output == allow


def test_scope_guard_keeps_denying_attestation_state_for_its_own_stronger_reason(
    tmp_path: Path,
) -> None:
    """ABSOLUTE is also checked before the coherence check, and must keep its own message:
    claims and receipts are denied because no ticket scope may EVER unlock them, not
    merely because this session's record happens to be broken today."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "forger", lambda rec: rec.__setitem__("session", "impostor"))

    result = _lib(proj, "forger", "hook-scope", stdin=json.dumps(
        {"session_id": "forger",
         "tool_input": {"file_path": str(proj / ".claude" / "evidence" / f"{TID}.json")}}))

    assert result.returncode == 0, result.stdout + result.stderr
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "harness-written attestation state" in reason


def test_scope_guard_is_unaffected_by_another_sessions_broken_record(
    tmp_path: Path,
) -> None:
    """Attribution is per-session (harness-protocol rule 8). One session's corrupt record
    must not constrain, or crash, anybody else -- the fail-closed rule keys off THIS
    session's record only."""
    proj = _make_project(tmp_path)
    _corrupt(proj, "theirs", lambda rec: rec.__setitem__("session", "impostor"))

    result = _lib(proj, "mine", "hook-scope", stdin=json.dumps(
        {"session_id": "mine", "tool_input": {"file_path": str(proj / "src" / "x.txt")}}))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""       # unclaimed sessions stay unconstrained


# ---------------------------------------------------------------------------
# The `integrity` CLI verb
# ---------------------------------------------------------------------------
def test_integrity_verb_reports_an_unanswerable_diff_distinctly_from_a_clean_one(
    tmp_path: Path,
) -> None:
    """`IntegrityUnavailable` escaped this call site uncaught, so the verb died with a
    raw traceback, printed NOTHING on stdout, and exited 1 -- the same code it uses for
    "these named files are out of scope". A caller reading stdout for findings saw an
    empty list, i.e. a pass. Exit 2 keeps "could not ask" distinct from "asked, and the
    answer was: these files"."""
    proj = _make_project(tmp_path)

    result = _lib(proj, "auditor", "integrity", TID, "deadbeef" * 5)

    assert result.returncode == 2, result.stdout + result.stderr
    _no_traceback(result)
    assert "cannot be evaluated" in result.stdout


def test_integrity_verb_still_exits_zero_silently_on_a_genuinely_clean_scope(
    tmp_path: Path,
) -> None:
    """Positive control: the new exit code must not have displaced the clean answer."""
    proj = _make_project(tmp_path)
    head = _git(proj, "rev-parse", "HEAD").stdout.strip()
    (proj / "src" / "feature.txt").write_text("in scope\n")

    result = _lib(proj, "auditor", "integrity", TID, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""
