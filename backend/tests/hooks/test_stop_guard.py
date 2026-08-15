"""T-13 acceptance: .claude/hooks/stop_guard.sh blocks a Stop only when the
CURRENT session's own ticket claim is unfinished.

Every test drives the REAL stop_guard.sh as a subprocess (see
conftest.run_stop_hook / conftest.stop_decision).

T-31 (v2 harness migration) rewrote this file end to end. v1's
stop_guard.sh resolved ownership by scanning a single, session-blind,
append-only .claude/active-ticket log via claim_lookup.py's --strict
resolution — "whose claim is the most recent line naming THIS session,
and is it still open". v2 has no log and no resolution algorithm at all:
harness_lib.py's cmd_hook("stop") is exactly

    if p.get("stop_hook_active"): return "", 0
    c = session_claim(p.get("session_id") or "")
    if not c: return "", 0
    ... block, naming c["ticket"] ...

i.e. does .claude/claims/<this payload's session_id>.json exist? That's
the entire algorithm. Every v1 test below existed to pin down a resolution
edge case (multiple sessions, unattributed lines, missing timestamps,
ordering) that v2's one-file-per-session model makes structurally
impossible rather than merely handled — see each test's own docstring for
the specific v1 -> v2 mapping; none of the eleven original behaviour
classes were dropped, they were re-expressed or superseded as noted.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import run_stop_hook, stop_decision, write_claim

SESSION_A = "session-aaaa-1111-current"
SESSION_B = "session-bbbb-2222-observer"
SESSION_C = "session-cccc-3333-unrelated"


def test_no_claim_file_allows(project: Path) -> None:
    """v1: test_no_active_ticket_file_allows. Unchanged in spirit: no claim
    record at all for this session -> allow.
    """
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_stop_hook_active_bypasses_claim_check(project: Path) -> None:
    """v1: test_stop_hook_active_guard_prevents_loop. Unchanged: the
    REQUIRED infinite-loop guard fires before any claim is even consulted
    (harness_lib.py checks stop_hook_active first, unconditionally),
    regardless of session or claim state.
    """
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, stop_hook_active=True, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_blocks_when_current_session_has_unfinished_claim(project: Path) -> None:
    """v1: test_blocks_when_current_session_has_unfinished_claim. Unchanged:
    this session's own open claim blocks its own Stop, naming the ticket.
    """
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "block"
    assert "T-5" in result.stdout


def test_another_sessions_claim_never_blocks_this_session(project: Path) -> None:
    """v1: test_allows_when_only_claim_belongs_to_another_session — T-13
    acceptance 2, THE observer case that fired on T-8, T-9 and T-11. This
    is now the ENTIRE algorithm rather than one resolved edge case: v2
    looks up .claude/claims/<payload session_id>.json directly, so a claim
    filed under a different session's name is not merely correctly
    resolved as "not mine" — it is never even read.
    """
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result) == "allow"


def test_evidence_never_overrides_an_open_claim(project: Path) -> None:
    """v1 had two tests here: test_allows_when_claimed_ticket_already_has_
    evidence (T-13 acceptance 4: a claim whose ticket already passed
    verify is not honoured as still-open -> allow) and
    test_evidence_for_a_different_ticket_does_not_satisfy_the_gate (evidence
    for the WRONG ticket doesn't count -> block). Both assumed stop_guard.sh
    consulted evidence at all.

    v2's cmd_hook("stop") never reads .claude/evidence/ — it only checks
    whether a claim file exists. This isn't an oversight: cmd_close removes
    the claim file in the SAME operation that writes the receipt, so under
    the real lifecycle "claim exists" and "not yet resolved" are the same
    fact, and checking evidence separately would be redundant. This test
    proves the stronger, simpler v2 guarantee that supersedes both v1 tests
    at once: with a live claim, the decision is IDENTICAL — always block —
    whether there's no evidence, evidence for the claimed ticket (a state
    the real lifecycle can't produce, since only cmd_close writes it and
    cmd_close also removes the claim), or evidence for some other ticket.
    """
    write_claim(project, "T-5", SESSION_A)
    evidence_dir = project / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)

    result_no_evidence = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_no_evidence) == "block"
    assert "T-5" in result_no_evidence.stdout

    (evidence_dir / "T-5.json").write_text('{"ticket":"T-5","commit":"abc"}\n')
    result_own_ticket_evidence = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_own_ticket_evidence) == "block"

    (evidence_dir / "T-5.json").unlink()
    (evidence_dir / "T-6.json").write_text('{"ticket":"T-6","commit":"def"}\n')
    result_other_ticket_evidence = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_other_ticket_evidence) == "block"


def test_concurrent_claims_resolve_independently_per_session(project: Path) -> None:
    """v1: test_own_claim_found_behind_a_more_recent_other_sessions_claim —
    "each session's Stop must key off its OWN most recent claim, not the
    globally-last line" of the shared append-only log. v2 has no shared
    log and no "most recent" to resolve: each session's claim is its own
    file, addressed by filename, so this now proves that MULTIPLE claim
    FILES coexisting in .claude/claims/ don't cross-contaminate — a
    concern that didn't meaningfully exist for a single global file, but
    is exactly the kind of bug a naive re-implementation of claims() could
    introduce (e.g. iterating the directory and matching the wrong entry).
    """
    write_claim(project, "T-5", SESSION_A, ts="2026-08-14T00:00:00Z")
    write_claim(project, "T-6", SESSION_B, ts="2026-08-14T01:00:00Z")

    result_a = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_a) == "block"
    assert "T-5" in result_a.stdout

    result_b = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result_b) == "block"
    assert "T-6" in result_b.stdout


def test_leftover_v1_active_ticket_artifact_is_completely_inert(project: Path) -> None:
    """v1 had three tests pinned to claim_lookup.py's --strict resolution
    of the shared .claude/active-ticket log's shape:
    test_legacy_bare_claim_line_never_blocks_any_session (an unattributed
    bare "T-9\\n" line never blocks anyone), test_legacy_claim_line_allows_
    regardless_of_evidence (same, evidence present or not), and
    test_third_session_not_matched_to_stale_legacy_line_behind_newer_claim
    (a third, unrelated session isn't confused by a legacy line sitting
    behind a newer, properly-attributed claim for someone else).

    v2 has no .claude/active-ticket concept at all — cmd_hook("stop") never
    opens that path, under any content, for any session. All three v1
    classes collapse onto one guarantee: a leftover v1-shaped artifact
    (whatever it contains) has ZERO effect on the v2 decision, combined
    here with a REAL v2 claim for a different session/ticket (mirroring
    the "third session, unrelated claim already present" shape of the
    original finding #5 scenario) and with evidence present too, to keep
    every sub-case genuinely exercised rather than trivially vacuous.
    """
    at_path = project / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-9\n")
    write_claim(project, "T-15", "fresh-session-abc-123", ts="2026-08-14T01:00:00Z")
    evidence_dir = project / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "T-9.json").write_text('{"ticket":"T-9"}\n')

    result_current = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_current) == "allow"

    result_observer = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result_observer) == "allow"

    result_third = run_stop_hook(project, session_id=SESSION_C)
    assert stop_decision(result_third) == "allow"


def test_unidentifiable_session_never_blocked_via_any_claim(project: Path) -> None:
    """v1 had two tests about an unidentifiable session (neither the Stop
    payload's session_id nor $CLAUDE_CODE_SESSION_ID available):
    test_session_with_no_timestamp_never_blocks_via_strict_mode (a claim
    record naming a real session but with no "ts" degrades to
    unattributed, since only claim.sh's own writer produces both together)
    and test_session_unidentifiable_degrades_to_global_check (the
    session-blind PRE-T-13 fallback: block if ANY claim exists, "never MORE
    permissive than before T-13").

    v2 has no "degrade to a global check" fallback, and it's a documented,
    intentional simplification, not a gap this ticket may quietly patch:
    harness_lib.py's cmd_hook("stop") is `session_claim(p.get("session_id")
    or "")` — a missing/None session_id becomes "", and "" can never match
    a real per-session claim filename, so this ALWAYS allows, regardless of
    how many claims exist for how many other identified sessions. This is
    the opposite of v1's "never more permissive" guarantee — a genuine,
    deliberate v1 -> v2 narrowing (per T31-brief.md's authoritative summary
    of harness_lib.py's contract), asserted here explicitly so a future
    change to this behaviour is a conscious decision, not an accident.
    There is no v2 analogue of "a claim record with no timestamp" to test
    separately: v2 claim identity is the FILENAME, not a parsed field
    inside a shared log, so that specific failure mode has no target left.
    """
    write_claim(project, "T-7", "some-recorded-session-not-mine")
    result = run_stop_hook(project, session_id=None)
    assert stop_decision(result) == "allow"


def test_release_removes_the_claim_so_stop_allows(project: Path) -> None:
    """v1: test_release_marker_means_no_open_claim — appending a release
    marker record after a claim line meant "no longer open". v2's
    cmd_release (and cmd_close) delete the claim file outright rather than
    appending a marker to a log that no longer exists; write_claim(...,
    ticket=None, ...) mirrors that deletion exactly.
    """
    write_claim(project, "T-5", SESSION_A, ts="2026-08-14T00:00:00Z")
    write_claim(project, None, SESSION_A, ts="2026-08-14T02:00:00Z")
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"
