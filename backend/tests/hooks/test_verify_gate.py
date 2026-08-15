"""T-31: coverage for the v2 replacement of the deleted v1 verify_gate.sh.

verify_gate.sh (a single script that resolved which ticket the current
session owned, ran its verify command, and stamped a bare-timestamp
`.claude/evidence/<id>.pass` on success) was deleted whole by the
"cc-factory: harness sync" commit. Its responsibility is now split across
TWO independent v2 entry points in `.claude/scripts/harness_lib.py`, both
covered here as self-contained, hermetic subprocess-driven tests:

  A. `harness_lib.py close` (via `.claude/scripts/claim.sh close`) is the
     ONLY thing that ever runs a ticket's verify command. It integrity-
     checks every file changed since the claim's `start_commit` against the
     ticket's scope + META_ALLOW + HARNESS_STATE, runs the verify, and only
     on a pass writes `.claude/evidence/<id>.json` (ticket/session/verify/
     commit/fingerprint/attempts/ts) and removes the claim. A failing
     verify increments `attempts` on the claim and mints no receipt.

  B. `harness_lib.py hook-taskgate` (wired to PreToolUse[TaskUpdate] and
     TaskCompleted in .claude/settings.json) NEVER runs a verify command at
     all -- it refuses (exit 2, stderr names the ticket) to let a
     `T-<n>`-prefixed task be marked `completed` unless
     `.claude/evidence/<id>.json` already exists, purely by checking the
     file's presence.

Every test below builds its own synthetic project in `tmp_path`: real git
repo, a hand-authored `docs/tickets.json` with cheap synthetic verify
commands, and copies of the real `.claude/scripts/` and
`.claude/hooks/task_gate.sh` -- never the shared `conftest.py` (another
session is concurrently rewriting it) and never the real repo's own
`.claude/claims/` or `.claude/evidence/` (this session holds a live claim on
T-31 there right now; see `_never_touch_the_real_repos_live_harness_state`
below).

v1 concepts with NO v2 analog, and why: v1's append-only, session-blind
`.claude/active-ticket` JSONL log supported "legacy claim line" amnesty, a
"global last claim" fallback for an unidentifiable session, and shadowing
of a stale legacy line by a newer real claim. v2 replaced that shared log
with one claim file per session (`.claude/claims/<session_id>.json`) keyed
directly off `$CLAUDE_CODE_SESSION_ID` -- there is no shared log left to
fall back to, resolve "last line" from, or shadow, so those specific
mechanisms have no v2 counterpart to test. What they were guarding against
(one session's completion accidentally acting on another session's, or on
nobody's, ticket) is exactly what the ownership tests below assert against
the new model instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_LIB = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"
CLAIM_SH = REPO_ROOT / ".claude" / "scripts" / "claim.sh"
GEN_TASKS = REPO_ROOT / ".claude" / "scripts" / "gen_tasks.py"
TASK_GATE_SH = REPO_ROOT / ".claude" / "hooks" / "task_gate.sh"

# Ticket ids used by this file's synthetic tickets.json documents. Chosen to
# be well outside the real plan's real range (docs/tickets.json today runs
# T-0..T-31) so they can never collide with a real ticket.
SYNTHETIC_TICKET_IDS = ("T-100", "T-101", "T-9999")


@pytest.fixture(autouse=True)
def _never_touch_the_real_repos_live_harness_state() -> None:
    """Hermeticity guard for the whole file.

    This session holds a LIVE claim on T-31 in the real repo's
    `.claude/claims/`, and other sessions may be concurrently claiming or
    closing OTHER real tickets while this suite runs -- so this can't just
    snapshot-and-diff the whole directory (that would be flaky under
    real, unrelated, concurrent harness activity). Instead it asserts,
    before and after every test, that none of THIS FILE's synthetic ticket
    ids ever shows up as a real claim or a real receipt: the one leak
    signature that would prove a test's subprocess calls escaped their
    `tmp_path` project (e.g. a missing/blank CLAUDE_PROJECT_DIR override).
    """

    def _check() -> None:
        for tid in SYNTHETIC_TICKET_IDS:
            assert not (REPO_ROOT / ".claude" / "evidence" / f"{tid}.json").exists()
        claims_dir = REPO_ROOT / ".claude" / "claims"
        if claims_dir.is_dir():
            for p in claims_dir.glob("*.json"):
                try:
                    doc = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                assert doc.get("ticket") not in SYNTHETIC_TICKET_IDS

    _check()
    yield
    _check()


# ---------------------------------------------------------------------------
# Synthetic-project builders
# ---------------------------------------------------------------------------
def _run_git(proj: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True, check=True)


def _ticket(
    tid: str,
    verify: Any,
    scope: list[str],
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "id": tid,
        "title": f"synthetic ticket {tid}",
        "objective": "exercise harness_lib.py's close/taskgate contract",
        "acceptance": ["synthetic acceptance criterion"],
        "scope": scope,
        "depends_on": depends_on or [],
    }
    if verify is not _NO_VERIFY_KEY:
        t["verify"] = verify
    return t


_NO_VERIFY_KEY = object()


def _tickets_doc(*tickets: dict[str, Any]) -> dict[str, Any]:
    return {"project": "verify-gate-test", "tickets": list(tickets)}


def _make_project(
    tmp_path: Path,
    tickets_doc: dict[str, Any],
    scope_files: dict[str, str],
) -> Path:
    """Build a disposable, hermetic CLAUDE_PROJECT_DIR: a real git repo,
    a synthetic docs/tickets.json, and real copies of the harness scripts
    and the task_gate.sh hook -- never the live repo's own state.

    ``scope_files``: {relative_path: content}, written and committed so the
    tickets' ``scope`` globs have real git-tracked files for
    scope_files()/fingerprint()/integrity() to operate over.
    """
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy2(HARNESS_LIB, proj / ".claude" / "scripts" / "harness_lib.py")
    shutil.copy2(CLAIM_SH, proj / ".claude" / "scripts" / "claim.sh")
    shutil.copy2(GEN_TASKS, proj / ".claude" / "scripts" / "gen_tasks.py")
    shutil.copy2(TASK_GATE_SH, proj / ".claude" / "hooks" / "task_gate.sh")
    (proj / "docs" / "tickets.json").write_text(json.dumps(tickets_doc))
    for rel, content in scope_files.items():
        fp = proj / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)

    _run_git(proj, "init", "-q")
    _run_git(proj, "config", "user.email", "hooktest@example.com")
    _run_git(proj, "config", "user.name", "hook-test")
    _run_git(proj, "add", "-A")
    _run_git(proj, "commit", "-q", "-m", "initial")
    return proj


def _head(proj: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _claim_file(proj: Path, session_id: str) -> Path:
    return proj / ".claude" / "claims" / f"{session_id}.json"


def _evidence_file(proj: Path, tid: str) -> Path:
    return proj / ".claude" / "evidence" / f"{tid}.json"


def _read_json(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Subprocess drivers -- all go through the real .claude/scripts/claim.sh and
# .claude/hooks/task_gate.sh wrapper scripts, exactly like the harness does.
# ---------------------------------------------------------------------------
def _env(proj: Path, session_id: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _claim(
    proj: Path, tid: str, note: str, session_id: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), "claim", tid, note],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=30,
    )


def _close(proj: Path, session_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), "close"],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=30,
    )


def _fingerprint(proj: Path, tid: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(proj / ".claude" / "scripts" / "harness_lib.py"), "fingerprint", tid],
        cwd=proj, capture_output=True, text=True, env=_env(proj, "session-fingerprint"),
        timeout=30,
    )


def _taskgate(
    proj: Path, payload: dict[str, Any], session_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "hooks" / "task_gate.sh")],
        input=json.dumps(payload), cwd=proj, capture_output=True, text=True,
        env=_env(proj, session_id), timeout=30,
    )


def _pretooluse_taskupdate(subject: str, status: str = "completed") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "TaskUpdate",
        "tool_input": {"subject": subject, "status": status},
    }


def _task_completed(subject: str) -> dict[str, Any]:
    return {"hook_event_name": "TaskCompleted", "task": {"subject": subject}}


# ---------------------------------------------------------------------------
# hook-taskgate: a non-matching subject / non-completed status is ignored
# ---------------------------------------------------------------------------
def test_taskgate_ignores_non_completed_status(tmp_path: Path) -> None:
    """v1: a PreToolUse[TaskUpdate] whose status wasn't "completed" was
    ignored (allow) regardless of tool_name -- verify_gate.sh never gated
    on an in-progress status change. v2: hook-taskgate only acts when
    tool_input.status == "completed"; anything else is a silent allow with
    no receipt lookup at all.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _taskgate(proj, _pretooluse_taskupdate("T-100: ship it", status="in_progress"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_taskgate_ignores_a_subject_naming_no_ticket(tmp_path: Path) -> None:
    """v1: a TaskUpdate/TaskCompleted whose subject didn't resolve to a
    ticket at all was never gated. v2: hook-taskgate regexes the subject
    for a leading "T-<n>"; a subject that doesn't match is a silent allow
    even when status == "completed" -- there is no ticket id to check a
    receipt for.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _taskgate(proj, _pretooluse_taskupdate("write the README", status="completed"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# close gating: no receipt -> blocked; a real receipt -> allowed
# ---------------------------------------------------------------------------
def test_taskgate_blocks_completion_without_a_receipt(tmp_path: Path) -> None:
    """v1: a ticket with no evidence file could never be marked complete
    (verify_gate.sh would run its verify and block, or fail closed on a
    bad verify string). v2: hook-taskgate refuses completion outright
    (exit 2, stderr names the ticket) purely because
    `.claude/evidence/T-100.json` is absent -- true for a ticket nobody
    ever claimed, exactly as for one still in progress.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _taskgate(proj, _task_completed("T-100: ship it"))
    assert result.returncode == 2
    assert "T-100" in result.stderr
    assert not _evidence_file(proj, "T-100").exists()


def test_taskgate_allows_completion_once_a_receipt_exists(tmp_path: Path) -> None:
    """Companion to the above, same ticket: after a real claim -> close
    lifecycle mints its receipt, task_gate.sh now allows the completion.
    This is the v2 replacement for v1's "ticket with existing evidence
    allows without rerunning verify" -- hook-taskgate never runs verify at
    all, in either state; it only ever checks receipt existence.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-close-then-complete"
    claim = _claim(proj, "T-100", "ordering note", sid)
    assert claim.returncode == 0, claim.stdout
    close = _close(proj, sid)
    assert close.returncode == 0, close.stdout

    result = _taskgate(proj, _task_completed("T-100: ship it"))
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_taskgate_never_invokes_the_verify_command_itself(tmp_path: Path) -> None:
    """v1: a ticket nobody had claimed was allowed through WITHOUT ever
    running its verify command (proven via a sentinel file the verify
    command only touches if it actually runs). v2 splits this out
    entirely: hook-taskgate correctly BLOCKS this claimed-but-never-closed
    ticket (no receipt yet), but it must reach that answer purely by
    checking for the evidence file -- never by shelling out to run the
    verify command to find out. The untouched sentinel proves it never ran.
    """
    sentinel = tmp_path / "ran-if-verify-executed"
    verify = f"touch {sentinel} && true"
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", verify, ["src/**"])), {"src/module.txt": "a"},
    )
    claim = _claim(proj, "T-100", "claimed but never closed", "session-claims-never-closes")
    assert claim.returncode == 0, claim.stdout

    result = _taskgate(proj, _task_completed("T-100: ship it"))
    assert result.returncode == 2
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# ownership: close acts only on the calling session's OWN claim
# ---------------------------------------------------------------------------
def test_close_with_no_claim_held_refuses(tmp_path: Path) -> None:
    """v1's session-unidentifiable / no-claim paths degraded to a global or
    legacy fallback resolution. v2 has no shared claim log to fall back to
    at all: a session with no claim file of its own simply cannot close
    anything, full stop.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _close(proj, "session-with-nothing-claimed")
    assert result.returncode == 1
    assert "no claim held" in result.stdout
    assert not _evidence_file(proj, "T-100").exists()


def test_close_acts_only_on_the_calling_sessions_own_claim(tmp_path: Path) -> None:
    """Re-expression of v1's cross-session isolation (a claim line for one
    session must never be actioned by another) against v2's one-claim-file-
    per-session model: session B holds no claim of its own, so it cannot
    close session A's claim on T-100 -- A's claim file and the ticket's
    unresolved status are both left completely untouched. Also checks the
    flip side ("another session's claim is never actionable"): B cannot
    even CLAIM T-100 while A holds it.
    """
    sentinel = tmp_path / "ran-if-b-closed-it"
    verify = f"touch {sentinel} && true"
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", verify, ["src/**"])), {"src/module.txt": "a"},
    )
    claim_a = _claim(proj, "T-100", "A's note", "session-A")
    assert claim_a.returncode == 0, claim_a.stdout
    before = _read_json(_claim_file(proj, "session-A"))

    claim_b = _claim(proj, "T-100", "B tries to steal it", "session-B")
    assert claim_b.returncode == 1
    assert "claimed by another session" in claim_b.stdout

    close_b = _close(proj, "session-B")
    assert close_b.returncode == 1
    assert "no claim held" in close_b.stdout
    assert not sentinel.exists()
    assert not _evidence_file(proj, "T-100").exists()
    assert _read_json(_claim_file(proj, "session-A")) == before


# ---------------------------------------------------------------------------
# evidence is written ONLY after a passing verify
# ---------------------------------------------------------------------------
def test_close_writes_evidence_only_after_a_passing_verify(tmp_path: Path) -> None:
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-passing"
    assert _claim(proj, "T-100", "note", sid).returncode == 0
    result = _close(proj, sid)
    assert result.returncode == 0, result.stdout
    assert _evidence_file(proj, "T-100").exists()
    assert not _claim_file(proj, sid).exists()


def test_close_with_failing_verify_mints_no_receipt_and_records_the_attempt(
    tmp_path: Path,
) -> None:
    """v1: a failing verify blocked completion and never wrote `.pass`. v2:
    cmd_close's failure path increments the claim's `attempts` counter and
    returns without ever writing to the evidence path -- the claim stays in
    place (not removed) so the session can retry or release it.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "false", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-failing"
    assert _claim(proj, "T-100", "note", sid).returncode == 0
    result = _close(proj, sid)
    assert result.returncode == 2
    assert "VERIFY FAIL" in result.stdout
    assert not _evidence_file(proj, "T-100").exists()
    claim_after = _read_json(_claim_file(proj, sid))
    assert claim_after["attempts"] == 1


# ---------------------------------------------------------------------------
# fail-closed: unknown ticket id, and a malformed/missing verify string
# ---------------------------------------------------------------------------
def test_taskgate_fails_closed_on_a_completely_unknown_ticket_id(tmp_path: Path) -> None:
    """v1: an unknown ticket, even when "owned" by the session, still
    failed closed ("no verify command found"). v2: hook-taskgate never
    consults docs/tickets.json at all -- it only asks whether
    `.claude/evidence/<id>.json` exists -- so a ticket id that was NEVER in
    the plan is refused exactly like one that's merely unresolved.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _taskgate(proj, _task_completed("T-9999: never existed in the plan"))
    assert result.returncode == 2
    assert "T-9999" in result.stderr


def test_claim_refuses_a_verify_string_that_fails_lint(tmp_path: Path) -> None:
    """v2 moves the "malformed verify" check earlier than close time, to
    claim time: cmd_claim runs LINT_RULES over the verify string before a
    claim file is ever written. An echo-only verify ("self-test evidence,
    not grounding evidence") is refused outright, so the ticket can
    structurally never reach close and mint a receipt.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "echo done", ["src/**"])), {"src/module.txt": "a"},
    )
    result = _claim(proj, "T-100", "note", "session-lint-echo")
    assert result.returncode == 1
    assert "lint" in result.stdout
    assert not _claim_file(proj, "session-lint-echo").exists()


def test_claim_refuses_a_verify_string_that_unconditionally_passes(tmp_path: Path) -> None:
    """Same claim-time lint gate, different malformed shape: a verify
    string ending in "|| true" would make the close-time verify run always
    report success no matter what it actually checked. Refused for the
    same structural reason as the echo-only case above.
    """
    proj = _make_project(
        tmp_path,
        _tickets_doc(_ticket("T-100", "some_check || true", ["src/**"])),
        {"src/module.txt": "a"},
    )
    result = _claim(proj, "T-100", "note", "session-lint-or-true")
    assert result.returncode == 1
    assert "lint" in result.stdout
    assert not _claim_file(proj, "session-lint-or-true").exists()


def test_claim_fails_closed_on_a_missing_or_null_verify_string(tmp_path: Path) -> None:
    """v1: a ticket with `"verify": null` failed closed once owned and
    gated. v2's cmd_claim doesn't special-case a null or absent `verify`
    with a friendly message -- it errors out while lint-checking it -- but
    the outward guarantee is identical to the required behaviour class: a
    nonzero exit and, critically, NO claim file is ever written, so a
    ticket with a missing or null verify string can never reach close and
    can never mint a receipt either way.
    """
    t_null = _ticket("T-100", "true", ["src/**"])
    t_null["verify"] = None
    t_missing = _ticket("T-101", _NO_VERIFY_KEY, ["src/**"])
    proj = _make_project(
        tmp_path, _tickets_doc(t_null, t_missing), {"src/module.txt": "a"},
    )

    r_null = _claim(proj, "T-100", "note", "session-null-verify")
    assert r_null.returncode != 0
    assert not _claim_file(proj, "session-null-verify").exists()

    r_missing = _claim(proj, "T-101", "note", "session-missing-verify")
    assert r_missing.returncode != 0
    assert not _claim_file(proj, "session-missing-verify").exists()


# ---------------------------------------------------------------------------
# the receipt binds to the tree it certifies (T-31 acceptance 4)
# ---------------------------------------------------------------------------
def test_receipt_commit_equals_repo_head_at_close_time(tmp_path: Path) -> None:
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-commit-binding"
    assert _claim(proj, "T-100", "note", sid).returncode == 0
    result = _close(proj, sid)
    assert result.returncode == 0, result.stdout
    receipt = _read_json(_evidence_file(proj, "T-100"))
    assert receipt["commit"] == _head(proj)


def test_fingerprint_changes_when_scope_file_content_changes(tmp_path: Path) -> None:
    """v1's bare `.pass` timestamp carried no proof of WHAT was verified --
    two different trees, verified at two different times, could produce
    byte-identical evidence. v2's `fingerprint` is a content hash over the
    ticket's scope files (harness_lib.fingerprint()); this is the guarantee
    T-31 acceptance 4 asks for, replacing v1's bare timestamp. Exercised
    directly via the `fingerprint` CLI verb rather than a second full
    claim/close cycle, since a ticket with an existing receipt can never be
    reclaimed.
    """
    proj = _make_project(
        tmp_path,
        _tickets_doc(_ticket("T-100", "true", ["src/**"])),
        {"src/module.txt": "original content"},
    )
    before = _fingerprint(proj, "T-100")
    assert before.returncode == 0

    (proj / "src" / "module.txt").write_text("materially different content")
    after = _fingerprint(proj, "T-100")
    assert after.returncode == 0

    assert before.stdout.strip() and after.stdout.strip()
    assert before.stdout.strip() != after.stdout.strip()


def test_receipt_fingerprint_matches_the_harness_computed_fingerprint(tmp_path: Path) -> None:
    """Direct binding proof: the fingerprint written into the receipt at
    close time is not a placeholder or a value computed some other way --
    it is exactly what harness_lib.py's own `fingerprint` verb recomputes
    from the (post-close) tree, on demand, afterwards.
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-fp-binding"
    assert _claim(proj, "T-100", "note", sid).returncode == 0
    assert _close(proj, sid).returncode == 0

    receipt = _read_json(_evidence_file(proj, "T-100"))
    recomputed = _fingerprint(proj, "T-100")
    assert recomputed.returncode == 0
    assert receipt["fingerprint"] == recomputed.stdout.strip()


# ---------------------------------------------------------------------------
# integrity: an out-of-scope change fails the close and mints no receipt
# ---------------------------------------------------------------------------
def test_close_fails_integrity_when_a_file_outside_scope_changed(tmp_path: Path) -> None:
    """v2's compensating control for the documented Bash hole: every file
    changed since the claim's start_commit must be inside the ticket's
    scope (or META_ALLOW/HARNESS_STATE). An out-of-scope file fails the
    close outright, names the offending path in the message, mints no
    receipt, and leaves the claim exactly as it was (not even `attempts`
    is touched -- the integrity check runs before the verify/fail path).
    """
    proj = _make_project(
        tmp_path, _tickets_doc(_ticket("T-100", "true", ["src/**"])), {"src/module.txt": "a"},
    )
    sid = "session-integrity"
    assert _claim(proj, "T-100", "note", sid).returncode == 0
    before = _read_json(_claim_file(proj, sid))

    rogue = proj / "outside" / "rogue.txt"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("out of scope edit")

    result = _close(proj, sid)
    assert result.returncode == 2
    assert "INTEGRITY FAIL" in result.stdout
    assert "outside/rogue.txt" in result.stdout
    assert not _evidence_file(proj, "T-100").exists()
    assert _read_json(_claim_file(proj, sid)) == before
