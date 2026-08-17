"""A claim record's FILENAME is its attribution -- and every reader must agree.

`.claude/claims/<session_id>.json` is written only by `harness_lib.cmd_claim`, only for
the session whose id names it, and every consumer (close, release, the scope guard, the
stop guard) resolves "who owns this ticket" from that path alone. So a record whose own
`session` field disagrees with its filename does not describe a weaker claim -- it
describes TWO contradictory owners, with nothing in the harness able to adjudicate
between them. `harness_lib.claim_defects()` calls that incoherent, and the rule this file
pins is: **an incoherent record is refused by name, never silently resolved in favour of
the filename.**

`test_close_unattributed_claim_gap.py` covers the close half (it is the T-28
characterization of exactly this defect, per-case). This file covers what that leaves
open -- the shape variants of the attribution field itself, the positive control that the
check is not a blanket refusal, and the three OTHER commands that read the same record:

  * close   -- a record with no `session` key at all, or a non-string one, is refused
               just like a mismatched one; a coherent one still closes normally.
  * release -- MUST still work on an incoherent record. An agent may not hand-edit
               `.claude/claims/**` (harness_lib.ABSOLUTE denies it unconditionally) and
               close now refuses the record, so release is the only sanctioned exit; if it
               refused too, a corrupt record would wedge the session with no way out.
               Release certifies nothing (it writes no evidence), so degrading here cannot
               launder anything -- it retires the record and tells the human, which is
               exactly where a corrupt record belongs.
  * guard   -- an incoherent record must not UNLOCK anything: a ticket scope it cannot be
               trusted to name is not a licence to write plan/harness files, so in-repo
               writes fail closed while it exists. `.claude/NEEDS_HUMAN.md` stays writable
               (META_ALLOW is checked first) so the session can report the problem.
  * stop    -- an incoherent record is still an OPEN one; letting the session stop
               silently on it is how a misattributed claim outlives the session that made
               it, so the stop guard blocks and names the record.

Self-contained, like `test_verify_gate.py` and `test_close_unattributed_claim_gap.py`:
every test builds its own synthetic git project in `tmp_path` (real git repo,
hand-authored `docs/tickets.json`, a copy of the real `.claude/scripts/`) and never
touches the real repo's `.claude/claims/`, `.claude/evidence/`, `docs/`, or git history --
sessions may hold live claims there while this suite runs.

Lifecycle verbs are driven through the real `.claude/scripts/claim.sh` (harness-protocol
rule 2). The two hooks are driven through `harness_lib.py hook-scope` / `hook-stop`, which
is precisely what the `.claude/hooks/*.sh` shims exec -- so these tests bind the hook
CONTRACT this change touches rather than the shell indirection in front of it.
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
HARNESS_LIB = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"
CLAIM_SH = REPO_ROOT / ".claude" / "scripts" / "claim.sh"
GEN_TASKS = REPO_ROOT / ".claude" / "scripts" / "gen_tasks.py"

# Well outside the real plan's range (T-0..T-31) so they can never collide with a real
# ticket, and distinct from the ids the neighbouring self-contained files use.
TID = "T-9200"
TID_PROTECTED = "T-9201"


@pytest.fixture(autouse=True)
def _never_touch_the_real_repos_live_harness_state() -> Iterator[None]:
    """Hermeticity trip-wire: other sessions may be claiming/closing real tickets while
    this suite runs, so this cannot snapshot-and-diff the whole directory. It asserts
    instead that this file's synthetic ticket ids never appear as a real claim or receipt
    -- the one leak signature proving a subprocess escaped its `tmp_path` project.
    """

    def _check() -> None:
        for tid in (TID, TID_PROTECTED):
            assert not (REPO_ROOT / ".claude" / "evidence" / f"{tid}.json").exists()
        claims_dir = REPO_ROOT / ".claude" / "claims"
        if claims_dir.is_dir():
            for p in claims_dir.glob("*.json"):
                try:
                    doc = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                assert doc.get("ticket") not in (TID, TID_PROTECTED)

    _check()
    yield
    _check()


# ---------------------------------------------------------------------------
# Synthetic project + drivers
# ---------------------------------------------------------------------------
def _run_git(proj: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True, check=True)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "claims").mkdir(parents=True)
    (proj / ".claude" / "evidence").mkdir(parents=True)
    (proj / "src").mkdir(parents=True)
    shutil.copy2(HARNESS_LIB, proj / ".claude" / "scripts" / "harness_lib.py")
    shutil.copy2(CLAIM_SH, proj / ".claude" / "scripts" / "claim.sh")
    shutil.copy2(GEN_TASKS, proj / ".claude" / "scripts" / "gen_tasks.py")

    def ticket(tid: str, scope: list[str]) -> dict[str, Any]:
        return {
            "id": tid,
            "title": f"synthetic ticket {tid}",
            "objective": "exercise claim-record attribution coherence",
            "acceptance": ["synthetic acceptance criterion"],
            "scope": scope,
            "depends_on": [],
            "verify": "true",
        }

    tickets_doc = {
        "project": "claim-attribution-coherence-test",
        "tickets": [
            ticket(TID, ["src/**"]),
            # A ticket whose contract explicitly names a PROTECTED path: the ONLY way the
            # guard ever lets a plan/harness file through (the D1 yield). Exactly the
            # unlock an incoherent record must not be able to borrow.
            ticket(TID_PROTECTED, ["src/**", ".claude/settings.json"]),
        ],
    }
    (proj / "docs" / "tickets.json").write_text(json.dumps(tickets_doc))
    (proj / "src" / "module.txt").write_text("a")
    (proj / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

    _run_git(proj, "init", "-q", "-b", "main")
    _run_git(proj, "config", "user.email", "hooktest@example.com")
    _run_git(proj, "config", "user.name", "hook-test")
    _run_git(proj, "config", "commit.gpgsign", "false")
    _run_git(proj, "add", "-A")
    _run_git(proj, "commit", "-q", "-m", "initial")
    return proj


def _env(proj: Path, session_id: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _claim_path(proj: Path, session_id: str) -> Path:
    return proj / ".claude" / "claims" / f"{session_id}.json"


def _evidence_path(proj: Path, tid: str = TID) -> Path:
    return proj / ".claude" / "evidence" / f"{tid}.json"


def _claim_sh(proj: Path, session_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), *args],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=30,
    )


def _claim(proj: Path, session_id: str, tid: str = TID) -> None:
    result = _claim_sh(proj, session_id, "claim", tid, "ordering note")
    assert result.returncode == 0, result.stdout + result.stderr


def _corrupt(proj: Path, session_id: str, mutate: Any) -> None:
    """Claim for real, then damage the resulting record -- so every case exercises a
    record that genuinely went through the lifecycle and then became incoherent, not a
    hand-forged fixture that never did.
    """
    path = _claim_path(proj, session_id)
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record))


def _hook(proj: Path, kind: str, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(proj / ".claude" / "scripts" / "harness_lib.py"), f"hook-{kind}"],
        input=json.dumps(payload), cwd=proj, capture_output=True, text=True,
        env=_env(proj), timeout=30,
    )


def _scope_decision(proj: Path, session_id: str, file_path: Path) -> tuple[str, str]:
    """(decision, reason) from the real hook-scope contract: silence is allow, a deny is
    JSON on stdout carrying a human-readable reason; the hook always exits 0.
    """
    result = _hook(proj, "scope", {
        "hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": session_id,
        "tool_input": {"file_path": str(file_path), "old_string": "a", "new_string": "b"},
    })
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return "allow", ""
    payload = json.loads(result.stdout)
    out = payload["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return "deny", out["permissionDecisionReason"]


def _stop_decision(proj: Path, session_id: str) -> tuple[str, str]:
    result = _hook(proj, "stop", {"hook_event_name": "Stop", "session_id": session_id})
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return "allow", ""
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    return "block", payload["reason"]


# ---------------------------------------------------------------------------
# close: the remaining unattributed shapes, and the positive control
# ---------------------------------------------------------------------------
def test_close_refuses_a_record_with_no_session_field_at_all(tmp_path: Path) -> None:
    """A record that names no owner of its own is unattributed in substance -- the case
    T-28 acceptance 1 is written about. It is refused by name, not quietly resolved from
    the filename, because "the filename is the attribution" is a rule about what the
    harness may TRUST, not a licence to fill in whatever the record omits.
    """
    proj = _make_project(tmp_path)
    sid = "session-no-session-key"
    _claim(proj, sid)
    _corrupt(proj, sid, lambda record: record.pop("session"))

    result = _claim_sh(proj, sid, "close")

    assert result.returncode == 1, result.stdout + result.stderr
    assert f".claude/claims/{sid}.json" in result.stdout
    assert "session" in result.stdout
    assert not _evidence_path(proj).exists()
    assert "Traceback" not in result.stderr


def test_close_refuses_a_record_whose_session_field_is_not_a_string(tmp_path: Path) -> None:
    """`"session": null` (or any non-string) can never equal a filename, so it is the
    mismatch case in another costume -- and the type check must come BEFORE the
    comparison, or a null would silently compare unequal and produce a confusing message
    instead of a precise one.
    """
    proj = _make_project(tmp_path)
    sid = "session-null-session-field"
    _claim(proj, sid)
    _corrupt(proj, sid, lambda record: record.__setitem__("session", None))

    result = _claim_sh(proj, sid, "close")

    assert result.returncode == 1, result.stdout + result.stderr
    assert f".claude/claims/{sid}.json" in result.stdout
    assert not _evidence_path(proj).exists()
    assert "Traceback" not in result.stderr


def test_a_coherent_record_still_closes_and_the_receipt_names_the_filename_session(
    tmp_path: Path,
) -> None:
    """The control that keeps every refusal above honest: an untouched record -- whose
    `session` field agrees with its filename, as `cmd_claim` always writes it -- still
    closes normally, mints the receipt, and retires the claim. Without this, the whole
    coherence check could be a blanket refusal and every test above would still pass.
    """
    proj = _make_project(tmp_path)
    sid = "session-coherent"
    _claim(proj, sid)

    result = _claim_sh(proj, sid, "close")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "closed" in result.stdout
    receipt = json.loads(_evidence_path(proj).read_text())
    assert receipt["ticket"] == TID
    assert receipt["session"] == sid
    assert not _claim_path(proj, sid).exists()


# ---------------------------------------------------------------------------
# release: the sanctioned exit from an incoherent record
# ---------------------------------------------------------------------------
def test_release_is_the_sanctioned_exit_for_a_misattributed_record(tmp_path: Path) -> None:
    """Refusing the close must not wedge the session. Release retires the record, and the
    NEEDS_HUMAN.md line carries BOTH the reason the agent gave and the defect itself, so
    the human learns a record was misattributed rather than just that a ticket was
    dropped.
    """
    proj = _make_project(tmp_path)
    sid = "session-releasing"
    _claim(proj, sid)
    _corrupt(proj, sid, lambda record: record.__setitem__("session", "impostor"))

    result = _claim_sh(proj, sid, "release", "claim record is misattributed")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _claim_path(proj, sid).exists()
    logged = (proj / ".claude" / "NEEDS_HUMAN.md").read_text()
    assert "claim record is misattributed" in logged
    assert f".claude/claims/{sid}.json" in logged
    assert "impostor" in logged
    assert not _evidence_path(proj).exists(), "release must never write evidence"


def test_release_also_retires_an_unparseable_record_and_names_it_to_the_human(
    tmp_path: Path,
) -> None:
    """The harshest case: the record cannot be read at all, so not even the ticket id is
    recoverable. Release must still succeed (it is the only exit) and must say plainly
    that the ticket could not be attributed rather than inventing one.
    """
    proj = _make_project(tmp_path)
    sid = "session-unparseable"
    _claim(proj, sid)
    _claim_path(proj, sid).write_text("{not valid json")

    result = _claim_sh(proj, sid, "release", "record is corrupt")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _claim_path(proj, sid).exists()
    logged = (proj / ".claude" / "NEEDS_HUMAN.md").read_text()
    assert "record is corrupt" in logged
    assert f".claude/claims/{sid}.json" in logged
    assert "unreadable" in logged
    assert "Traceback" not in result.stderr


def test_release_still_reports_no_claim_when_there_is_genuinely_no_record(
    tmp_path: Path,
) -> None:
    """Absence and incoherence must stay distinguishable in both directions: with no file
    at all, release still refuses with the plain "no claim" answer rather than degrading.
    """
    proj = _make_project(tmp_path)

    result = _claim_sh(proj, "session-never-claimed", "release", "nothing to release")

    assert result.returncode == 1
    assert "no claim held by this session" in result.stdout


# ---------------------------------------------------------------------------
# scope guard: an incoherent record unlocks nothing
# ---------------------------------------------------------------------------
def test_scope_guard_denies_in_repo_writes_while_the_record_is_incoherent(
    tmp_path: Path,
) -> None:
    """The record's ticket -- and so the scope every write is judged against -- comes from
    a record the harness cannot attribute. Fail closed, name the record, and keep
    `.claude/NEEDS_HUMAN.md` writable so the session can report it (META_ALLOW is checked
    before this, deliberately). The in-scope control proves this is a change of state, not
    a guard that denies `src/**` regardless.
    """
    proj = _make_project(tmp_path)
    sid = "session-guarded"
    _claim(proj, sid)
    in_scope = proj / "src" / "module.txt"
    assert _scope_decision(proj, sid, in_scope) == ("allow", "")

    _corrupt(proj, sid, lambda record: record.__setitem__("session", "impostor"))

    verdict, reason = _scope_decision(proj, sid, in_scope)
    assert verdict == "deny"
    assert f".claude/claims/{sid}.json" in reason
    assert "impostor" in reason
    assert _scope_decision(proj, sid, proj / ".claude" / "NEEDS_HUMAN.md") == ("allow", "")


def test_an_incoherent_record_cannot_unlock_a_protected_path(tmp_path: Path) -> None:
    """The security-relevant half. A PROTECTED plan/harness path opens ONLY when the
    CLAIMED ticket's own scope names it, so the unlock is only as trustworthy as the claim
    record it is read from. With a coherent claim on a ticket whose contract names
    `.claude/settings.json`, the guard allows it; corrupt that record's attribution and
    the same write is denied -- a record that cannot say who owns it cannot sanction an
    edit to the plan or the harness.
    """
    proj = _make_project(tmp_path)
    sid = "session-protected"
    _claim(proj, sid, TID_PROTECTED)
    settings = proj / ".claude" / "settings.json"
    assert _scope_decision(proj, sid, settings) == ("allow", "")

    _corrupt(proj, sid, lambda record: record.__setitem__("session", "impostor"))

    verdict, reason = _scope_decision(proj, sid, settings)
    assert verdict == "deny"
    assert f".claude/claims/{sid}.json" in reason


# ---------------------------------------------------------------------------
# stop guard: an incoherent record is still an open one
# ---------------------------------------------------------------------------
def test_stop_guard_blocks_and_names_the_record_when_it_is_incoherent(
    tmp_path: Path,
) -> None:
    """A session may not stop quietly on a record it can neither close nor certify; the
    block names the record and points at release. The no-claim control proves the block is
    caused by the corrupt record rather than by the hook blocking unconditionally.
    """
    proj = _make_project(tmp_path)
    sid = "session-stopping"
    _claim(proj, sid)
    verdict, reason = _stop_decision(proj, sid)
    assert verdict == "block"
    assert TID in reason                       # the ordinary open-claim block

    _corrupt(proj, sid, lambda record: record.__setitem__("session", "impostor"))

    verdict, reason = _stop_decision(proj, sid)
    assert verdict == "block"
    assert f".claude/claims/{sid}.json" in reason
    assert "impostor" in reason
    assert "release" in reason
    assert _stop_decision(proj, "session-never-claimed") == ("allow", "")


def test_stop_guard_blocks_on_an_unparseable_record_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """`session_claim`'s bare `json.load` used to propagate a JSONDecodeError straight out
    of the hook, which surfaces as a hook error rather than a decision -- the session stops
    with a corrupt, still-open claim behind it. It now blocks with a named reason.
    """
    proj = _make_project(tmp_path)
    sid = "session-stop-unparseable"
    _claim(proj, sid)
    _claim_path(proj, sid).write_text("")

    verdict, reason = _stop_decision(proj, sid)

    assert verdict == "block"
    assert f".claude/claims/{sid}.json" in reason
    assert "unreadable" in reason
