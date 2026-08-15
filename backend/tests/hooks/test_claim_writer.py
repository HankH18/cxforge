"""T-31 migration: v1's production claim WRITER (``.claude/hooks/claim.sh``,
deleted by the c44f9af harness sync) unconditionally appended one JSONL
line per invocation to a shared ``.claude/active-ticket`` log. v2's writer
is ``.claude/scripts/harness_lib.py`` (invoked through the thin
``.claude/scripts/claim.sh`` wrapper, same entrypoint name, entirely
different contract): instead of always appending, it is a gate -- most of
its job is REFUSING to write, for reasons the v1 writer never had a
concept of at all (dependencies, receipts, verify-string lint), because a
v1 claim was just an attribution record, not a lifecycle transition.

Coverage mapping (v1 behaviour class -> v2 assertion):
  * v1 test_first_claim_creates_one_well_formed_record /
    test_second_claim_appends_without_disturbing_the_first_line (the
    writer's core "write a well-formed record" duty) ->
    test_successful_claim_makes_a_start_commit_and_a_claim_file. (Full
    field-by-field shape assertions live in test_claim_format.py; this
    file's angle is the ACT of writing succeeding end to end.)
  * v1 test_refuses_to_write_with_no_identifiable_session (the ONE v1
    refusal case) -> v2 has an entire refusal taxonomy, since a claim now
    gates a real lifecycle transition rather than just recording who
    touched what:
      - test_second_claim_from_same_session_is_refused_naming_held_ticket
        (one-claim-per-session; NEW in v2 -- v1 let one session write
        unlimited claim lines)
      - test_second_session_cannot_claim_a_ticket_another_session_holds
        (one-session-per-ticket; NEW in v2)
      - test_unknown_ticket_id_is_refused (NEW: v1 had no ticket registry
        to validate against)
      - test_claiming_a_ticket_that_already_has_a_receipt_is_refused (NEW:
        v1 had no receipt concept)
      - test_unmet_dependency_is_refused_naming_the_missing_dependency
        (NEW: v1 had no dependency graph)
      - test_verify_string_lint_refuses_a_dangerous_verify_command (NEW:
        v1's record carried no verify command to lint)
  * v1 test_release_marker_writes_null_ticket (release appended a
    ``{"ticket": null, ...}`` marker line to the SAME shared log) ->
    test_release_removes_the_claim_and_logs_a_reason_to_needs_human (v2
    has no ticket=null marker to write -- releasing simply removes the
    session's claim file outright, since there is no shared log for a
    tombstone to matter to; the reason instead goes to the durable
    human-facing ``.claude/NEEDS_HUMAN.md`` channel).
  * v1 test_explicit_session_argument_overrides_env_var /
    test_written_claim_round_trips_through_claim_lookup_owned_mode /
    test_timestamp_is_a_real_parseable_utc_iso8601_string: session
    resolution, round-tripping and timestamp shape are now asserted in
    test_claim_format.py (the record's shape, not the writer's refusal
    behaviour), since v2 has no separate "lookup" tool to round-trip
    through -- reading .claude/claims/<session>.json back IS the
    round-trip.

Self-contained: builds its own synthetic git project per test in
tmp_path (git init, one commit, a hand-authored docs/tickets.json, and a
copy of the real .claude/scripts + .claude/hooks trees) and drives it via
.claude/scripts/claim.sh with CLAUDE_PROJECT_DIR/CLAUDE_CODE_SESSION_ID
pointed at that synthetic project. Never touches the real repo's
.claude/claims/, .claude/evidence/, docs/, or git history -- this
session holds a live claim on T-31 there.
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
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t31-writer-test",
    "GIT_AUTHOR_EMAIL": "t31-writer-test@example.invalid",
    "GIT_COMMITTER_NAME": "t31-writer-test",
    "GIT_COMMITTER_EMAIL": "t31-writer-test@example.invalid",
}


def make_ticket(
    tid: str,
    *,
    scope: list[str] | None = None,
    verify: str = "true",
    depends_on: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": tid,
        "title": title or f"Synthetic ticket {tid}",
        "objective": "synthetic test ticket",
        "acceptance": ["n/a"],
        "verify": verify,
        "scope": scope if scope is not None else [f"{tid}.txt"],
        "depends_on": depends_on or [],
        "non_goals": [],
        "parallel_safe": False,
        "status": "open",
    }


def tickets_doc(*tickets: dict[str, Any]) -> dict[str, Any]:
    return {"project": "t31-writer-test", "tickets": list(tickets)}


def make_repo(tmp_path: Path, doc: dict[str, Any]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "tickets.json").write_text(json.dumps(doc))
    shutil.copytree(
        REAL_SCRIPTS_DIR, repo / ".claude" / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        REAL_HOOKS_DIR, repo / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, env=env, check=True)
    return repo


def run_claim_sh(
    repo: Path, *args: str, session: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV}
    env["CLAUDE_PROJECT_DIR"] = str(repo)
    env["CLAUDE_CODE_SESSION_ID"] = session
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(repo / ".claude" / "scripts" / "claim.sh"), *args],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


def do_claim(repo: Path, tid: str, note: str, *, session: str) -> subprocess.CompletedProcess[str]:
    return run_claim_sh(repo, "claim", tid, note, session=session)


def do_close(repo: Path, *, session: str) -> subprocess.CompletedProcess[str]:
    return run_claim_sh(repo, "close", session=session)


def do_release(repo: Path, reason: str, *, session: str) -> subprocess.CompletedProcess[str]:
    return run_claim_sh(repo, "release", reason, session=session)


def claims_dir(repo: Path) -> Path:
    return repo / ".claude" / "claims"


def read_claim(repo: Path, session: str) -> dict[str, Any] | None:
    p = claims_dir(repo) / f"{session}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def commit_count(repo: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV}, check=True,
    )
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------
# The writer succeeds: makes a start commit AND a claim file
# ---------------------------------------------------------------------------
def test_successful_claim_makes_a_start_commit_and_a_claim_file(tmp_path: Path) -> None:
    """v1: test_first_claim_creates_one_well_formed_record proved claim.sh
    appended a well-formed JSONL line. v2's writer does two things atomically
    on success: an empty `ticket-start: <tid>` commit (record shape proved in
    test_claim_format.py), AND a claim file. This test is about the writer's
    observable side effects succeeding at all, end to end via the real
    entrypoint (.claude/scripts/claim.sh), not the record's internal shape.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-5")))
    before = commit_count(repo)

    result = do_claim(repo, "T-5", "first in queue order", session="session-aaaa")

    assert result.returncode == 0, result.stderr
    assert commit_count(repo) == before + 1
    assert read_claim(repo, "session-aaaa") is not None
    assert "T-5" in result.stdout


# ---------------------------------------------------------------------------
# One-claim-per-session
# ---------------------------------------------------------------------------
def test_second_claim_from_same_session_is_refused_naming_held_ticket(tmp_path: Path) -> None:
    """NEW in v2 (v1's writer had no concept of "one claim per session" --
    it happily appended an unlimited number of lines for the same session).
    A session that already owns a claim is refused a second one, and the
    refusal names the ticket it currently holds so the caller knows what to
    close/release first.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1"), make_ticket("T-2")))
    first = do_claim(repo, "T-1", "note one", session="session-aaaa")
    assert first.returncode == 0, first.stderr

    second = do_claim(repo, "T-2", "note two", session="session-aaaa")
    assert second.returncode != 0
    assert "T-1" in second.stdout

    # The original claim is completely undisturbed and no claim exists for T-2.
    original = read_claim(repo, "session-aaaa")
    assert original is not None
    assert original["ticket"] == "T-1"
    assert original["note"] == "note one"


# ---------------------------------------------------------------------------
# One-session-per-ticket
# ---------------------------------------------------------------------------
def test_second_session_cannot_claim_a_ticket_another_session_holds(tmp_path: Path) -> None:
    """NEW in v2. Another session's claim is never treated as available --
    a second session attempting to claim an already-claimed ticket is
    refused, and the refusal names the session already holding it.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1")))
    first = do_claim(repo, "T-1", "session A got here first", session="session-aaaa")
    assert first.returncode == 0, first.stderr

    second = do_claim(repo, "T-1", "session B also wants it", session="session-bbbb")
    assert second.returncode != 0
    assert "session-aaaa" in second.stdout

    assert read_claim(repo, "session-bbbb") is None
    held = read_claim(repo, "session-aaaa")
    assert held is not None and held["ticket"] == "T-1"


# ---------------------------------------------------------------------------
# Unknown ticket id
# ---------------------------------------------------------------------------
def test_unknown_ticket_id_is_refused(tmp_path: Path) -> None:
    """NEW in v2: v1's writer took any ticket id as an opaque string and
    never consulted a plan; v2's writer validates against docs/tickets.json.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1")))
    result = do_claim(repo, "T-999", "no such ticket", session="session-aaaa")

    assert result.returncode != 0
    assert "T-999" in result.stdout
    assert read_claim(repo, "session-aaaa") is None


# ---------------------------------------------------------------------------
# Ticket already has a receipt
# ---------------------------------------------------------------------------
def test_claiming_a_ticket_that_already_has_a_receipt_is_refused(tmp_path: Path) -> None:
    """NEW in v2: v1 had no receipt/resolution concept at all. A ticket
    that has already gone through a real close (and so has a genuine v2
    receipt) cannot be claimed again.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1", verify="true")))
    claimed = do_claim(repo, "T-1", "do it for real", session="session-aaaa")
    assert claimed.returncode == 0, claimed.stderr
    closed = do_close(repo, session="session-aaaa")
    assert closed.returncode == 0, closed.stderr
    assert (repo / ".claude" / "evidence" / "T-1.json").exists()

    reclaim = do_claim(repo, "T-1", "try again", session="session-bbbb")
    assert reclaim.returncode != 0
    assert "already has a receipt" in reclaim.stdout
    assert read_claim(repo, "session-bbbb") is None


# ---------------------------------------------------------------------------
# Unmet dependency
# ---------------------------------------------------------------------------
def test_unmet_dependency_is_refused_naming_the_missing_dependency(tmp_path: Path) -> None:
    """NEW in v2: v1 had no dependency graph. A ticket whose depends_on
    entry has no receipt yet blocks the claim, and the refusal names the
    specific missing dependency (not just "blocked").
    """
    repo = make_repo(
        tmp_path,
        tickets_doc(make_ticket("T-1"), make_ticket("T-2", depends_on=["T-1"])),
    )
    result = do_claim(repo, "T-2", "wants to jump the queue", session="session-aaaa")

    assert result.returncode != 0
    assert "T-1" in result.stdout
    assert read_claim(repo, "session-aaaa") is None

    # Resolving the dependency for real unblocks the claim.
    dep_claim = do_claim(repo, "T-1", "do the dependency first", session="session-aaaa")
    assert dep_claim.returncode == 0, dep_claim.stderr
    dep_close = do_close(repo, session="session-aaaa")
    assert dep_close.returncode == 0, dep_close.stderr

    unblocked = do_claim(repo, "T-2", "now it should work", session="session-aaaa")
    assert unblocked.returncode == 0, unblocked.stderr


# ---------------------------------------------------------------------------
# Verify-string lint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("verify", "expected_substring"),
    [
        pytest.param("pytest -q || true", "|| true", id="or-true-swallow"),
        pytest.param("cd backend && pytest -q", "cd", id="bare-cd-and"),
        pytest.param("echo all good", "echo", id="echo-only"),
    ],
)
def test_verify_string_lint_refuses_a_dangerous_verify_command(
    tmp_path: Path, verify: str, expected_substring: str
) -> None:
    """NEW in v2: v1's claim record carried no verify command, so there was
    nothing to lint. v2's claim gates on harness_lib.LINT_RULES so a plan
    defect (a verify string that can never fail) is caught at claim time,
    before any work happens, rather than silently rubber-stamping a close
    later.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1", verify=verify)))
    result = do_claim(repo, "T-1", "should never get this far", session="session-aaaa")

    assert result.returncode != 0
    assert expected_substring in result.stdout
    assert read_claim(repo, "session-aaaa") is None
    assert commit_count(repo) == 1, "a lint-refused claim must not make a ticket-start commit"


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
def test_release_removes_the_claim_and_logs_a_reason_to_needs_human(tmp_path: Path) -> None:
    """v1: test_release_marker_writes_null_ticket proved release appended a
    ``{"ticket": null, ...}`` tombstone line to the SAME shared log, which a
    later --mode last/owned query would then resolve to "nothing". v2 has
    no shared log for a tombstone to matter to: releasing simply deletes
    the session's claim file outright (the ticket goes straight back to
    "queue" -- proved in test_claim_format.py's derived-status test), and
    the human-facing reason is written durably to .claude/NEEDS_HUMAN.md
    instead of being encoded as a null-ticket record only claim_lookup.py
    knew how to interpret.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1")))
    claimed = do_claim(repo, "T-1", "will need to bail", session="session-aaaa")
    assert claimed.returncode == 0, claimed.stderr

    result = do_release(repo, "plan defect: scope is wrong", session="session-aaaa")

    assert result.returncode == 0, result.stderr
    assert read_claim(repo, "session-aaaa") is None

    needs_human = repo / ".claude" / "NEEDS_HUMAN.md"
    assert needs_human.exists()
    content = needs_human.read_text()
    assert "T-1" in content
    assert "session-aaaa" in content
    assert "plan defect: scope is wrong" in content
