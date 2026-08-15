"""T-31 migration: the v1 append-only JSONL claim LEDGER and its parser
(``.claude/hooks/claim_lookup.py``, deleted by the c44f9af harness sync)
have no v2 counterpart to drive directly. This file rebinds the v1
behaviour classes claim_lookup.py used to guard against the v2 CONTRACT
that replaced them, driven end to end through the real
``.claude/scripts/claim.sh`` / ``harness_lib.py`` lifecycle:

  * v1 "one JSON record per line, ticket/session/ts all recoverable" ->
    v2 "one JSON *file* per session at .claude/claims/<session>.json,
    with a richer required shape (ticket, session, note, start_commit,
    attempts, ts)" -- test_claim_record_shape_has_all_required_fields,
    test_start_commit_field_is_the_real_ticket_start_commit.
  * v1 "--mode owned resolves the most recent line for THIS session,
    ignoring everyone else's lines" (per-session attribution over a
    shared log) -> v2 "attribution is structural: a session's claim
    lives at its own path, and only its own path" --
    test_claim_belongs_to_exactly_one_session_and_files_are_isolated.
  * v1 "--mode last / --mode owned resolve a *shared* ledger by
    interpretation" -> v2 "status is DERIVED (receipt -> resolved, claim
    -> in_progress, neither -> queue), never parsed from a log" --
    test_derived_status_queue_then_in_progress_then_resolved. This is
    the direct replacement for v1's "last line wins" ledger resolution.
  * v1 "legacy bare `T-13` line is honoured with session-agnostic
    amnesty until shadowed by a newer line" -> in v2 the harness sync
    itself created the analogous legacy artifact: 18 pre-migration
    receipts moved to bare-epoch ``.claude/evidence-v1/<T-id>.pass``
    files. Migration policy (T-31) is the OPPOSITE of v1's amnesty: a
    v1 record is permanently INERT, never honoured, never upgraded --
    only a real v2 JSON receipt resolves a ticket to "resolved". See
    test_legacy_v1_pass_record_is_inert_while_a_v2_json_receipt_is_honoured
    (T-31 acceptance 2/4).
  * v1 "missing active-ticket file / malformed line never crashes the
    parser, resolves to nothing" -> in v2 there is no shared mutable log
    for a stray byte to corrupt, so nothing but harness_lib.py itself
    ever writes into .claude/claims/ or .claude/evidence/; the residual
    "no state yet" case is the directories not existing at all, which
    must still resolve every ticket to "queue" without error --
    test_no_claims_or_evidence_directories_yet_resolves_cleanly_to_queue.
    (v1's "malformed JSON *inside* a claim file" and "ordering across
    many lines in one shared log" have no v2 analogue to test honestly:
    v2 claims are one-per-session, so there is no shared log to order,
    and no code path other than harness_lib.py's own atomic
    ``json.dump`` ever produces a claims-directory entry.)

Lifecycle REFUSALS (unknown ticket / duplicate claim / missing
dependency / receipt-already-exists / verify-string lint / release) are
covered in test_claim_writer.py, which is about the act of writing (or
refusing to write) a record. This file is about the record's shape and
what the harness derives from it.

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
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t31-format-test",
    "GIT_AUTHOR_EMAIL": "t31-format-test@example.invalid",
    "GIT_COMMITTER_NAME": "t31-format-test",
    "GIT_COMMITTER_EMAIL": "t31-format-test@example.invalid",
}


def make_ticket(
    tid: str,
    *,
    scope: list[str] | None = None,
    verify: str = "true",
    depends_on: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """A minimal, valid docs/tickets.json entry -- every field harness_lib.py
    actually reads (id, scope, verify, depends_on), plus the cosmetic ones
    gen_tasks.py (invoked at the end of a successful close) also expects.
    """
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
    return {"project": "t31-format-test", "tickets": list(tickets)}


def make_repo(tmp_path: Path, doc: dict[str, Any]) -> Path:
    """Build a disposable git project: init, docs/tickets.json, copies of
    the real .claude/scripts and .claude/hooks trees, one initial commit.
    """
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


def status_board(repo: Path) -> dict[str, str]:
    """Run claim.sh with no verb (defaults to status_board) and parse the
    "<id>  <status>  <title>" lines into {ticket_id: status}.
    """
    result = run_claim_sh(repo, session="status-board-probe")
    assert result.returncode == 0, result.stderr
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        tid, status_word, _title = line.split(maxsplit=2)
        out[tid] = status_word
    return out


def read_claim(repo: Path, session: str) -> dict[str, Any] | None:
    p = repo / ".claude" / "claims" / f"{session}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV}, check=True,
    )
    return result.stdout.strip()


def git_subject(repo: Path, commit: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%s", commit], cwd=repo, capture_output=True,
        text=True, env={**os.environ, **GIT_ENV}, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Shape / required fields
# ---------------------------------------------------------------------------
def test_claim_record_shape_has_all_required_fields(tmp_path: Path) -> None:
    """v1: test_claim_record_round_trips_ticket_session_and_timestamp
    asserted a JSONL line == {"ticket", "session", "ts"} exactly.
    v2: a claim record is a JSON object at .claude/claims/<session>.json
    with a strictly larger required shape -- {"ticket", "session", "note",
    "start_commit", "attempts", "ts"} -- since there is no shared log to
    lean on for ordering/attribution/audit; those all move into the
    record itself.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1")))
    before = int(time.time())
    result = do_claim(repo, "T-1", "picked first per plan order", session="session-A")
    after = int(time.time())
    assert result.returncode == 0, result.stderr

    record = read_claim(repo, "session-A")
    assert record is not None, ".claude/claims/session-A.json must exist after a successful claim"
    assert record.keys() == {"ticket", "session", "note", "start_commit", "attempts", "ts"}
    assert record["ticket"] == "T-1"
    assert record["session"] == "session-A"
    assert record["note"] == "picked first per plan order"
    assert record["attempts"] == 0
    assert isinstance(record["start_commit"], str) and record["start_commit"]
    # "ts" carries forward v1's timestamp-recoverability guarantee, minus
    # v1's cross-line ORDERING (moot: a session holds at most one claim at
    # a time, so there is nothing to order against).
    assert isinstance(record["ts"], int)
    assert before <= record["ts"] <= after


def test_start_commit_field_is_the_real_ticket_start_commit(tmp_path: Path) -> None:
    """New in v2 (no v1 analogue): a claim is bound to a real git commit,
    not just a timestamp, so close-time integrity can diff "everything
    that changed during this ticket" against the ticket's scope. This
    test proves start_commit is not just A commit but THE commit claim.sh
    itself made, carrying the exact `ticket-start: <tid>` message.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-7")))
    result = do_claim(repo, "T-7", "ordering note", session="session-A")
    assert result.returncode == 0, result.stderr

    record = read_claim(repo, "session-A")
    assert record is not None
    assert record["start_commit"] == git_head(repo)
    assert git_subject(repo, record["start_commit"]) == "ticket-start: T-7"


# ---------------------------------------------------------------------------
# Per-session attribution
# ---------------------------------------------------------------------------
def test_claim_belongs_to_exactly_one_session_and_files_are_isolated(tmp_path: Path) -> None:
    """v1: test_mode_owned_finds_most_recent_matching_line /
    test_mode_owned_returns_nothing_for_a_session_that_never_claimed
    asserted --mode owned resolved a session's OWN most recent line and
    nothing else out of a shared log.
    v2 replaces log-scanned attribution with structural attribution: each
    session's claim lives at its own path (.claude/claims/<session>.json)
    and nowhere else. This proves two sessions claiming two different
    tickets never see, borrow, or get attributed each other's record --
    "another session's claim is never treated as yours".
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1"), make_ticket("T-2")))

    r_a = do_claim(repo, "T-1", "A's ticket", session="session-A")
    assert r_a.returncode == 0, r_a.stderr
    r_b = do_claim(repo, "T-2", "B's ticket", session="session-B")
    assert r_b.returncode == 0, r_b.stderr

    claim_a = read_claim(repo, "session-A")
    claim_b = read_claim(repo, "session-B")
    assert claim_a is not None and claim_a["ticket"] == "T-1" and claim_a["session"] == "session-A"
    assert claim_b is not None and claim_b["ticket"] == "T-2" and claim_b["session"] == "session-B"

    # Nothing named after a session that never claimed exists at all.
    assert read_claim(repo, "session-C") is None

    # The claims directory holds exactly the two files, named by session.
    claims_dir = repo / ".claude" / "claims"
    assert sorted(p.stem for p in claims_dir.glob("*.json")) == ["session-A", "session-B"]

    board = status_board(repo)
    assert board["T-1"] == "in_progress"
    assert board["T-2"] == "in_progress"


# ---------------------------------------------------------------------------
# Derived status (replaces v1's "last line wins" ledger resolution)
# ---------------------------------------------------------------------------
def test_derived_status_queue_then_in_progress_then_resolved(tmp_path: Path) -> None:
    """v1 had no derived-status concept at all -- a consumer resolved the
    shared ledger itself via claim_lookup.py's --mode last ("whichever
    line is newest wins") or --mode owned. v2 removes the ledger and
    replaces that interpretation step with an explicit, stateless
    derivation living in harness_lib.status(): receipt exists -> resolved;
    else a claim names the ticket -> in_progress; else -> queue. This is
    the direct successor to v1's ledger-resolution tests, asserted across
    a full real lifecycle rather than a synthetic log.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-3", verify="true")))

    assert status_board(repo)["T-3"] == "queue"

    claimed = do_claim(repo, "T-3", "next in order", session="session-A")
    assert claimed.returncode == 0, claimed.stderr
    assert status_board(repo)["T-3"] == "in_progress"

    closed = do_close(repo, session="session-A")
    assert closed.returncode == 0, closed.stderr
    assert status_board(repo)["T-3"] == "resolved"

    # Resolution also retires the claim file and mints a v2 receipt.
    assert read_claim(repo, "session-A") is None
    receipt_path = repo / ".claude" / "evidence" / "T-3.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text())
    assert receipt["ticket"] == "T-3"
    assert receipt.keys() == {
        "ticket", "session", "verify", "commit", "fingerprint", "attempts", "ts",
    }


# ---------------------------------------------------------------------------
# LEGACY: v1 .pass records are inert; only v2 JSON receipts are honoured
# (T-31 acceptance 2/4)
# ---------------------------------------------------------------------------
def test_legacy_v1_pass_record_is_inert_while_a_v2_json_receipt_is_honoured(
    tmp_path: Path,
) -> None:
    """v1: test_legacy_bare_line_is_a_ticket_with_no_session and friends
    asserted claim_lookup.py granted a bare legacy `T-13` line
    session-agnostic AMNESTY -- it was honoured as a real claim.
    v2's migration policy is the opposite for the analogous artifact: the
    c44f9af harness sync moved 18 pre-migration receipts to bare-epoch
    ``.claude/evidence-v1/<T-id>.pass`` files (no commit, no fingerprint,
    no session). harness_lib.receipt() only ever reads
    ``.claude/evidence/<T-id>.json`` -- a v1 .pass file is retained as
    inert history, NEVER honoured, NEVER upgraded (T-31 non_goals
    forbids fabricating a commit/fingerprint from a bare timestamp).

    This proves both halves of T-31 acceptance 2/4 in one real lifecycle:
    (1) a ticket with only a v1 .pass record still derives as "queue" and
    remains genuinely claimable; (2) actually running the real v2
    lifecycle for it produces a receipt that IS honoured -- status flips
    to "resolved" and a second claim is refused because a receipt exists.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-9", verify="true")))
    v1_dir = repo / ".claude" / "evidence-v1"
    v1_dir.mkdir(parents=True)
    (v1_dir / "T-9.pass").write_text("1690000000\n")

    # (1) legacy record is inert: still "queue", still claimable.
    assert status_board(repo)["T-9"] == "queue"
    claimed = do_claim(repo, "T-9", "legacy record must not block this", session="session-A")
    assert claimed.returncode == 0, claimed.stderr
    assert status_board(repo)["T-9"] == "in_progress"

    # (2) the real v2 receipt, once minted, IS honoured.
    closed = do_close(repo, session="session-A")
    assert closed.returncode == 0, closed.stderr
    assert status_board(repo)["T-9"] == "resolved"
    assert (repo / ".claude" / "evidence" / "T-9.json").exists()
    # The untouched v1 artifact is still sitting there, still inert.
    assert (v1_dir / "T-9.pass").read_text() == "1690000000\n"

    reclaim = do_claim(repo, "T-9", "should be refused now", session="session-B")
    assert reclaim.returncode != 0
    assert "already has a receipt" in reclaim.stdout


# ---------------------------------------------------------------------------
# No shared mutable log left to corrupt: absent state resolves cleanly
# ---------------------------------------------------------------------------
def test_no_claims_or_evidence_directories_yet_resolves_cleanly_to_queue(tmp_path: Path) -> None:
    """v1: test_missing_file_resolves_to_nothing_never_crashes and the
    append-check malformed-payload tests proved claim_lookup.py degraded
    gracefully when .claude/active-ticket was absent, empty, or garbage.
    v2 has no single shared file that can be "missing" or "malformed" in
    that sense -- .claude/claims/ and .claude/evidence/ are plain
    directories, created lazily, and nothing but harness_lib.py's own
    json.dump ever writes into them. The residual "no state recorded
    yet" case is simply neither directory existing at all; this proves
    that resolves every ticket to "queue" without error, never crashing
    status_board the way a naive path.exists()-less read might.
    """
    repo = make_repo(tmp_path, tickets_doc(make_ticket("T-1"), make_ticket("T-2")))
    assert not (repo / ".claude" / "claims").exists()
    assert not (repo / ".claude" / "evidence").exists()

    board = status_board(repo)
    assert board == {"T-1": "queue", "T-2": "queue"}
