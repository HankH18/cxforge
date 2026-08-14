"""T-13 acceptance: .claude/hooks/stop_guard.sh blocks a Stop only when the
CURRENT session's own most recent ticket claim is unfinished.

Every test drives the REAL stop_guard.sh as a subprocess (see
conftest.run_stop_hook / conftest.stop_decision) — the claim-log FORMAT
itself is tested once, directly, in test_claim_format.py; this file only
exercises stop_guard.sh's own decisions.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_stop_hook, stop_decision, write_claim

SESSION_A = "session-aaaa-1111-current"
SESSION_B = "session-bbbb-2222-observer"


def test_no_active_ticket_file_allows(project: Path) -> None:
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_stop_hook_active_guard_prevents_loop(project: Path) -> None:
    """The REQUIRED infinite-loop guard fires before any claim is even
    consulted, regardless of session or claim state.
    """
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, stop_hook_active=True, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_blocks_when_current_session_has_unfinished_claim(project: Path) -> None:
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "block"
    assert "T-5" in result.stdout


def test_allows_when_only_claim_belongs_to_another_session(project: Path) -> None:
    """T-13 acceptance 2 — THE observer case that fired on T-8, T-9 and T-11:
    a second session in the same working directory must never be told to
    finish or revert a ticket it never claimed. SESSION_A holds an
    unfinished claim on T-5; SESSION_B's own Stop event must not even
    mention it.
    """
    write_claim(project, "T-5", SESSION_A)
    result = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result) == "allow"


def test_allows_when_claimed_ticket_already_has_evidence(project: Path) -> None:
    """T-13 acceptance 4: a claim whose ticket already passed verify is not
    honoured as still-open.
    """
    write_claim(project, "T-5", SESSION_A)
    evidence_dir = project / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "T-5.pass").write_text("1700000000\n")
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_evidence_for_a_different_ticket_does_not_satisfy_the_gate(project: Path) -> None:
    write_claim(project, "T-5", SESSION_A)
    evidence_dir = project / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "T-6.pass").write_text("1700000000\n")
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "block"
    assert "T-5" in result.stdout


def test_own_claim_found_behind_a_more_recent_other_sessions_claim(project: Path) -> None:
    """Append-only log with two sessions interleaved: each session's Stop
    must key off its OWN most recent claim, not the globally-last line.
    """
    write_claim(project, "T-5", SESSION_A, ts="2026-08-14T00:00:00Z")
    write_claim(project, "T-6", SESSION_B, ts="2026-08-14T01:00:00Z")

    result_a = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_a) == "block"
    assert "T-5" in result_a.stdout

    result_b = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result_b) == "block"
    assert "T-6" in result_b.stdout


def test_legacy_bare_claim_line_never_blocks_any_session(project: Path) -> None:
    """T-13 REPAIR (was test_legacy_bare_claim_line_blocks_any_session_
    absent_evidence — deliberately rewritten, not just relaxed; see
    justification below): a legacy line (no session recorded — exactly the
    pre-T-13 .claude/active-ticket shape, and exactly what this repo's own
    file contained the moment T-13 shipped) has NO attributable owner.

    The original version of this test asserted the OPPOSITE — that such a
    line blocks EVERY session — as a "documented, deliberate exception to
    acceptance 2". Adversarial review (finding #1) reproduced that exact
    assertion live against this repo's real, unmigrated .claude/active-
    ticket and identified it as the acceptance-2 bug itself, not a
    legitimate exception to it: acceptance 2 says a second session must
    NEVER be blocked by another session's claim, with no carve-out for
    "unless the claim is unattributed". stop_guard.sh now resolves
    ownership with claim_lookup.py's --strict flag, which never grants an
    unattributed line amnesty (see stop_guard.sh's own header for why this
    is safe: it only costs a Stop-time nag, never verify_gate.sh's actual
    completion gate). Both a would-be "current" session and a would-be
    "observer" session must see this as NO open claim.
    """
    at_path = project / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-9\n")

    result_a = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result_a) == "allow"

    result_b = run_stop_hook(project, session_id=SESSION_B)
    assert stop_decision(result_b) == "allow"


def test_legacy_claim_line_allows_regardless_of_evidence(project: Path) -> None:
    """Companion to test_legacy_bare_claim_line_never_blocks_any_session:
    since --strict means a legacy line is never "this session's claim" in
    the first place, whether evidence exists for it is moot — allow either
    way. (Originally this test's point was "evidence satisfies the gate for
    a legacy claim"; under --strict there is no gate to satisfy for an
    unattributed line at all, so both presence and absence of evidence are
    asserted here to keep the case meaningfully covered rather than
    trivially vacuous.)
    """
    at_path = project / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-9\n")

    no_evidence = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(no_evidence) == "allow"

    evidence_dir = project / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "T-9.pass").write_text("1700000000\n")

    with_evidence = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(with_evidence) == "allow"


def test_third_session_not_matched_to_stale_legacy_line_behind_newer_claim(
    project: Path,
) -> None:
    """T-13 adversarial finding #5: a legacy line followed by a NEWER,
    properly-attributed claim for a different ticket must not be handed to
    a third, unrelated session as if it were that session's own claim. This
    reproduces the exact scenario finding #5 demonstrated live: the
    ambiguity of an old legacy line is over "from then on" once anything
    real has been appended after it — see claim_lookup.py's resolve_owned().
    """
    at_path = project / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-13\n")
    write_claim(project, "T-15", "fresh-session-abc-123", ts="2026-08-14T01:00:00Z")

    result = run_stop_hook(project, session_id="totally-different-observer-session-999")
    assert stop_decision(result) == "allow"


def test_session_with_no_timestamp_never_blocks_via_strict_mode(project: Path) -> None:
    """T-13 adversarial finding #4, exercised at the GUARD level (not just
    claim_lookup.py directly): a record naming a real "session" but with NO
    "ts" at all cannot have come from .claude/hooks/claim.sh (the only
    production writer, which always writes both together) and earns no
    ownership trust — it degrades to unattributed, exactly like a bare
    legacy line, so --strict must never block ANY session on it, including
    the very session named in the record.
    """
    at_path = project / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text(json.dumps({"ticket": "T-5", "session": SESSION_A}) + "\n")

    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"


def test_session_unidentifiable_degrades_to_global_check(project: Path) -> None:
    """Neither the Stop payload's session_id nor $CLAUDE_CODE_SESSION_ID is
    available: this must degrade to the PRE-T-13 global check
    (session-blind), never to a silent allow — an unidentifiable session
    can only be as strict as before T-13, never more permissive.
    """
    write_claim(project, "T-7", "some-recorded-session-not-mine")
    result = run_stop_hook(project, session_id=None)
    assert stop_decision(result) == "block"
    assert "T-7" in result.stdout


def test_release_marker_means_no_open_claim(project: Path) -> None:
    write_claim(project, "T-5", SESSION_A, ts="2026-08-14T00:00:00Z")
    write_claim(project, None, SESSION_A, ts="2026-08-14T02:00:00Z")
    result = run_stop_hook(project, session_id=SESSION_A)
    assert stop_decision(result) == "allow"
