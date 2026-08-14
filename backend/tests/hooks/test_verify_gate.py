"""T-13 acceptance: .claude/hooks/verify_gate.sh gates and stamps evidence
ONLY for a ticket the CURRENT session has itself claimed.

Every test drives the REAL verify_gate.sh as a subprocess (see
conftest.run_verify_hook / conftest.verify_decision). Uses a SYNTHETIC
tickets.json (via conftest.make_project) with deterministic verify
commands, rather than the real docs/tickets.json's actual (slow, real)
verify commands — this file is testing verify_gate.sh's own gating logic,
not re-running the whole build's test suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import make_project, run_verify_hook, verify_decision, write_claim

SESSION_A = "session-aaaa-1111-current"
SESSION_B = "session-bbbb-2222-observer"


def _synthetic_project(tmp_path: Path, sentinel: Path) -> Path:
    """T-100 always passes verify; T-101 always fails. Both write a sentinel
    file if (and only if) their verify command actually RUNS, so tests can
    distinguish "never ran" (session-scoping correctly withheld the gate)
    from "ran and failed" (a real block).
    """
    tickets = {
        "tickets": [
            {
                "id": "T-100",
                "scope": ["x"],
                "verify": f"touch {sentinel}.T-100 && true",
            },
            {
                "id": "T-101",
                "scope": ["x"],
                "verify": f"touch {sentinel}.T-101 && false",
            },
            {"id": "T-102", "scope": ["x"], "verify": None},
        ]
    }
    return make_project(tmp_path, tickets)


@pytest.fixture
def vproject(tmp_path: Path) -> Path:
    sentinel = tmp_path / "ran"
    return _synthetic_project(tmp_path, sentinel)


def _ran(tmp_path: Path, ticket: str) -> bool:
    return (tmp_path / f"ran.{ticket}").exists()


# ---------------------------------------------------------------------------
# Baseline (non-T-13) behaviour, preserved from the original script — this
# hook had zero direct test coverage before T-13, so these are included
# alongside the session-scoping tests rather than assumed.
# ---------------------------------------------------------------------------
def test_pretooluse_wrong_tool_is_ignored(vproject: Path, tmp_path: Path) -> None:
    result = run_verify_hook(
        vproject, shape="pretooluse", tool_name="Edit", subject="T-100", session_id=SESSION_A,
    )
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-100")


def test_pretooluse_non_completed_status_is_ignored(vproject: Path, tmp_path: Path) -> None:
    result = run_verify_hook(
        vproject, shape="pretooluse", status="in_progress", subject="T-100", session_id=SESSION_A,
    )
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-100")


# ---------------------------------------------------------------------------
# T-13 acceptance 1 + 2: explicit-subject ownership
# ---------------------------------------------------------------------------
def test_explicit_subject_owned_by_current_session_runs_verify_and_passes(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-100", SESSION_A)
    result = run_verify_hook(vproject, subject="T-100: ship it", session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")
    assert (vproject / ".claude" / "evidence" / "T-100.pass").exists()


def test_explicit_subject_owned_by_current_session_runs_verify_and_fails(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-101", SESSION_A)
    result = run_verify_hook(vproject, subject="T-101: ship it", session_id=SESSION_A)
    assert verify_decision(result) == "block"
    assert _ran(tmp_path, "T-101")
    assert not (vproject / ".claude" / "evidence" / "T-101.pass").exists()


def test_explicit_subject_not_owned_by_current_session_is_allowed_without_running_verify(
    vproject: Path, tmp_path: Path,
) -> None:
    """T-13 acceptance 2, applied to verify_gate.sh: SESSION_B claims T-100
    (the passing ticket); a TaskCompleted for SESSION_A naming T-101 (the
    FAILING ticket) via an explicit subject must not be gated at all — it
    is not SESSION_A's claim. Proven by the sentinel: if this hook had
    wrongly run T-101's verify, ran.T-101 would exist.
    """
    write_claim(vproject, "T-100", SESSION_B)
    result = run_verify_hook(vproject, subject="T-101: not mine", session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-101")
    assert not (vproject / ".claude" / "evidence" / "T-101.pass").exists()


def test_explicit_subject_for_a_ticket_nobody_has_claimed_is_allowed_without_running_verify(
    vproject: Path, tmp_path: Path,
) -> None:
    result = run_verify_hook(vproject, subject="T-101: nobody claimed this", session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-101")


# ---------------------------------------------------------------------------
# T-13 acceptance 1 + 2: no-subject fallback
# ---------------------------------------------------------------------------
def test_fallback_no_subject_uses_current_sessions_own_claim(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-100", SESSION_A)
    result = run_verify_hook(vproject, subject=None, session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")


def test_fallback_no_subject_and_no_own_claim_allows_silently(
    vproject: Path, tmp_path: Path,
) -> None:
    """A pure status-change TaskUpdate from a session with nothing claimed
    (or only another session's claim on record) must not accidentally gate
    on — or write evidence for — someone else's in-progress ticket. This is
    exactly the fallback-path shape of the acceptance-2 observer bug.
    """
    write_claim(vproject, "T-101", SESSION_B)
    result = run_verify_hook(vproject, subject=None, session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-101")
    assert not (vproject / ".claude" / "evidence" / "T-101.pass").exists()


def test_fallback_no_subject_finds_own_claim_behind_a_more_recent_others(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-100", SESSION_A, ts="2026-08-14T00:00:00Z")
    write_claim(vproject, "T-101", SESSION_B, ts="2026-08-14T01:00:00Z")
    result = run_verify_hook(vproject, subject=None, session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")
    assert not _ran(tmp_path, "T-101")


# ---------------------------------------------------------------------------
# T-13 acceptance 4: a ticket that already has passing evidence
# ---------------------------------------------------------------------------
def test_ticket_with_existing_evidence_allows_without_rerunning_verify(
    vproject: Path, tmp_path: Path,
) -> None:
    """T-101's verify always fails — if this hook re-ran it, the ticket
    would (correctly, on the merits) be blocked. Pre-existing evidence must
    short-circuit that: the guard's job is already durably satisfied.
    """
    write_claim(vproject, "T-101", SESSION_A)
    evidence_dir = vproject / ".claude" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_path = evidence_dir / "T-101.pass"
    evidence_path.write_text("1700000000\n")

    result = run_verify_hook(vproject, subject="T-101: already done", session_id=SESSION_A)

    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-101")
    # Byte-identical: proves verify was never re-invoked (a re-run would
    # overwrite this with a fresh `date -u +%s` timestamp).
    assert evidence_path.read_text() == "1700000000\n"


# ---------------------------------------------------------------------------
# Unknown / unverifiable ticket must still fail closed when genuinely owned
# ---------------------------------------------------------------------------
def test_unknown_ticket_owned_by_current_session_still_fails_closed(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-999", SESSION_A)
    result = run_verify_hook(vproject, subject="T-999: made up ticket", session_id=SESSION_A)
    assert verify_decision(result) == "block"
    assert "no verify command found" in result.stderr


def test_ticket_with_null_verify_owned_by_current_session_fails_closed(
    vproject: Path, tmp_path: Path,
) -> None:
    write_claim(vproject, "T-102", SESSION_A)
    result = run_verify_hook(vproject, subject="T-102: null verify", session_id=SESSION_A)
    assert verify_decision(result) == "block"


# ---------------------------------------------------------------------------
# Session-unidentifiable degrade path: must reproduce PRE-T-13 (unscoped)
# behaviour exactly, never a NEW silent allow.
# ---------------------------------------------------------------------------
def test_session_unidentifiable_explicit_subject_used_directly_no_ownership_check(
    vproject: Path, tmp_path: Path,
) -> None:
    """Pre-T-13 parity: with no session identifiable at all, an explicit
    subject is gated directly with no ownership cross-check — exactly the
    original script's behaviour. T-101 fails verify -> block, even though
    nobody "owns" it in the new sense.
    """
    result = run_verify_hook(vproject, subject="T-101: unattributable", session_id=None)
    assert verify_decision(result) == "block"
    assert _ran(tmp_path, "T-101")


def test_session_unidentifiable_fallback_uses_global_last_claim(
    vproject: Path, tmp_path: Path,
) -> None:
    """Pre-T-13 parity for the no-subject fallback: falls back to the
    log's globally-last line (claim_lookup.py --mode last), session-blind —
    identical in shape to the original `head -n1 .claude/active-ticket`.
    """
    write_claim(vproject, "T-100", "some-other-recorded-session")
    result = run_verify_hook(vproject, subject=None, session_id=None)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")


# ---------------------------------------------------------------------------
# Legacy claim line (migration, T-13 constraint 7)
# ---------------------------------------------------------------------------
def test_legacy_bare_claim_line_is_owned_by_every_session(
    vproject: Path, tmp_path: Path,
) -> None:
    """Deliberately unchanged by the T-13 repair: this is the bounded
    MIGRATION-window amnesty (claim_lookup.py's resolve_owned(), non-strict)
    — a legacy line is only "owned by whoever asks" while it remains the
    SINGLE most recent line in the whole log, which is exactly this test's
    setup. verify_gate.sh keeps this (unlike stop_guard.sh's --strict)
    because flipping it would silently stop running verify for real,
    in-flight completions with no proof the exposure it would close is even
    reachable in practice — see verify_gate.sh's own header for the full
    reasoning, and the two tests below for the scenario that IS fixed.
    """
    at_path = vproject / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-100\n")

    result = run_verify_hook(vproject, subject=None, session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")
    assert (vproject / ".claude" / "evidence" / "T-100.pass").exists()


# ---------------------------------------------------------------------------
# T-13 adversarial finding #5: a legacy line shadowed by a NEWER, properly-
# attributed claim must not be handed to a third, unrelated session.
# ---------------------------------------------------------------------------
def test_fallback_third_session_not_matched_to_stale_legacy_line_behind_newer_claim(
    vproject: Path, tmp_path: Path,
) -> None:
    """Reproduces finding #5's concrete verify_gate.sh repro: a legacy line
    ("T-100"), then a newer real claim for a DIFFERENT session/ticket, then
    a third, totally unrelated session's pure status-change TaskUpdate
    (no-subject fallback) must not resolve to — and must not run verify
    for — the stale legacy ticket.
    """
    at_path = vproject / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-100\n")
    write_claim(vproject, "T-101", "fresh-session-abc-123", ts="2026-08-14T01:00:00Z")

    result = run_verify_hook(
        vproject, subject=None, session_id="totally-different-observer-session-999",
    )
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-100")
    assert not _ran(tmp_path, "T-101")


def test_explicit_subject_third_session_not_matched_to_stale_legacy_line_behind_newer_claim(
    vproject: Path, tmp_path: Path,
) -> None:
    """Same scenario via the explicit-subject cross-check path: a third
    session's TaskUpdate naming the STALE legacy ticket explicitly must
    still be refused once a newer claim has been recorded — the cross-check
    (TID == claim_lookup.py's --mode owned answer) must not fabricate a
    match against a shadowed legacy line either.
    """
    at_path = vproject / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text("T-100\n")
    write_claim(vproject, "T-101", "fresh-session-abc-123", ts="2026-08-14T01:00:00Z")

    result = run_verify_hook(
        vproject,
        subject="T-100: not really mine",
        session_id="totally-different-observer-session-999",
    )
    assert verify_decision(result) == "allow"
    assert not _ran(tmp_path, "T-100")


# ---------------------------------------------------------------------------
# T-13 adversarial finding #4: a session recorded with no timestamp earns
# no ownership trust — proven at the guard level, not just claim_lookup.py.
# ---------------------------------------------------------------------------
def test_fallback_session_with_no_timestamp_does_not_gate_for_an_unrelated_session(
    vproject: Path, tmp_path: Path,
) -> None:
    """A raw record naming SESSION_B but with no "ts" cannot have come from
    claim.sh (the only production writer) and must not be trusted as
    SESSION_B's exclusive claim: it degrades to unattributed, so it is
    subject to the SAME single-most-recent-line amnesty as a bare legacy
    line — resolvable by anyone while it is the log's last line, exactly
    like the (deliberately preserved) migration-window behaviour above, but
    it must never be treated as SESSION_B's own claim to the exclusion of
    others (there is no genuine attribution to exclude on).
    """
    at_path = vproject / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text(json.dumps({"ticket": "T-100", "session": SESSION_B}) + "\n")

    result = run_verify_hook(vproject, subject=None, session_id=SESSION_A)
    assert verify_decision(result) == "allow"
    assert _ran(tmp_path, "T-100")
