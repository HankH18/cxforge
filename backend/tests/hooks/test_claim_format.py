"""T-13 acceptance: the claim-log FORMAT itself — append-only, one JSON
record per line, ticket id + session id + UTC timestamp all recoverable —
plus claim_lookup.py's (the shared parser every guard calls) interpretation
of it, including the legacy single-line compatibility case.

This file tests the format/parser directly; test_stop_guard.py and
test_verify_gate.py test each hook's DECISION built on top of it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .conftest import CLAIM_LOOKUP_PATH, LIVE_ACTIVE_TICKET, REPO_ROOT, write_claim


def _lookup(claims_file: Path, *args: str) -> str:
    result = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(claims_file), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"claim_lookup.py must never crash: {args}, stderr={result.stderr!r}"
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
def test_claim_record_round_trips_ticket_session_and_timestamp(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-42", "session-abc-123", ts="2026-08-14T12:00:00Z")
    at_path = tmp_path / ".claude" / "active-ticket"
    lines = at_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "ticket": "T-42",
        "session": "session-abc-123",
        "ts": "2026-08-14T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------
def test_second_claim_appends_a_line_and_the_earlier_line_is_byte_identical(
    tmp_path: Path,
) -> None:
    write_claim(tmp_path, "T-1", "session-A", ts="2026-08-14T00:00:00Z")
    at_path = tmp_path / ".claude" / "active-ticket"
    first_write_content = at_path.read_text()

    write_claim(tmp_path, "T-2", "session-B", ts="2026-08-14T01:00:00Z")
    content = at_path.read_text()
    lines = content.splitlines(keepends=True)

    assert len(lines) == 2
    assert lines[0] == first_write_content, "earlier line must be byte-identical after append"
    assert json.loads(lines[1]) == {
        "ticket": "T-2",
        "session": "session-B",
        "ts": "2026-08-14T01:00:00Z",
    }


def test_three_claims_preserve_full_history_in_order(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-1", "session-A", ts="t1")
    write_claim(tmp_path, "T-2", "session-B", ts="t2")
    write_claim(tmp_path, "T-3", "session-A", ts="t3")
    at_path = tmp_path / ".claude" / "active-ticket"
    records = [json.loads(ln) for ln in at_path.read_text().splitlines()]
    assert [r["ticket"] for r in records] == ["T-1", "T-2", "T-3"]
    assert [r["session"] for r in records] == ["session-A", "session-B", "session-A"]


# ---------------------------------------------------------------------------
# claim_lookup.py interpretation
# ---------------------------------------------------------------------------
def test_mode_last_ignores_session_entirely(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-1", "session-A")
    write_claim(tmp_path, "T-2", "session-B")
    at_path = tmp_path / ".claude" / "active-ticket"
    assert _lookup(at_path, "--mode", "last") == "T-2"


def test_mode_owned_finds_most_recent_matching_line(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-1", "session-A", ts="t1")
    write_claim(tmp_path, "T-2", "session-B", ts="t2")
    write_claim(tmp_path, "T-3", "session-A", ts="t3")
    at_path = tmp_path / ".claude" / "active-ticket"
    assert _lookup(at_path, "--mode", "owned", "--session", "session-A") == "T-3"
    assert _lookup(at_path, "--mode", "owned", "--session", "session-B") == "T-2"


def test_mode_owned_returns_nothing_for_a_session_that_never_claimed(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-1", "session-A")
    at_path = tmp_path / ".claude" / "active-ticket"
    assert _lookup(at_path, "--mode", "owned", "--session", "session-Z") == ""


def test_release_marker_ticket_null_resolves_to_nothing(tmp_path: Path) -> None:
    write_claim(tmp_path, "T-1", "session-A", ts="t1")
    write_claim(tmp_path, None, "session-A", ts="t2")
    at_path = tmp_path / ".claude" / "active-ticket"
    assert _lookup(at_path, "--mode", "last") == ""
    assert _lookup(at_path, "--mode", "owned", "--session", "session-A") == ""


# ---------------------------------------------------------------------------
# Legacy compatibility (T-13 constraint 7 / migration)
# ---------------------------------------------------------------------------
def test_legacy_bare_line_is_a_ticket_with_no_session(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-13\n")
    assert _lookup(at_path, "--mode", "last") == "T-13"
    assert _lookup(at_path, "--mode", "owned", "--session", "any-session") == "T-13"
    assert _lookup(at_path, "--mode", "owned", "--session", "a-totally-different-one") == "T-13"


def test_legacy_line_with_no_trailing_newline_still_parses(tmp_path: Path) -> None:
    """Exactly what pathlib's write_text("T-5") produces — no trailing
    newline — which is also exactly the shape the pre-existing 113
    scope_guard.sh tests write via run_hook(active_ticket=...).
    """
    at_path = tmp_path / "at"
    at_path.write_text("T-5")
    assert _lookup(at_path, "--mode", "last") == "T-5"


def test_whitespace_only_content_resolves_to_nothing(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("   \n")
    assert _lookup(at_path, "--mode", "last") == ""


def test_missing_file_resolves_to_nothing_never_crashes(tmp_path: Path) -> None:
    at_path = tmp_path / "does-not-exist"
    assert _lookup(at_path, "--mode", "last") == ""
    assert _lookup(at_path, "--mode", "owned", "--session", "x") == ""


def test_legacy_line_amnesty_ends_once_a_newer_line_is_appended(tmp_path: Path) -> None:
    """T-13 adversarial finding #5: --mode owned's amnesty for an
    unattributed (legacy) line applies ONLY while it is the single most
    recent line in the whole log. The instant anything is appended after
    it — attributed or not, for any ticket — a fresh query no longer
    resolves to it.
    """
    at_path = tmp_path / "at"
    at_path.write_text("T-13\n")
    assert _lookup(at_path, "--mode", "owned", "--session", "any-unrelated-session") == "T-13"

    with at_path.open("a") as f:
        f.write(json.dumps({"ticket": "T-15", "session": "fresh-session", "ts": "t2"}) + "\n")

    # The legacy T-13 line is shadowed now; an unrelated session gets
    # nothing back for it (NOT "T-13", and NOT "T-15" either — T-15 belongs
    # to "fresh-session", not to this querying session).
    assert _lookup(at_path, "--mode", "owned", "--session", "any-unrelated-session") == ""
    assert _lookup(at_path, "--mode", "owned", "--session", "fresh-session") == "T-15"


def test_mode_owned_strict_never_grants_legacy_amnesty(tmp_path: Path) -> None:
    """T-13 adversarial finding #1: --strict (stop_guard.sh's mode) refuses
    an unattributed line for EVERY session, even as the log's sole line —
    no migration-window amnesty at all.
    """
    at_path = tmp_path / "at"
    at_path.write_text("T-13\n")
    assert _lookup(at_path, "--mode", "owned", "--session", "any-session", "--strict") == ""

    with at_path.open("a") as f:
        f.write(json.dumps({"ticket": "T-14", "session": "session-Q", "ts": "t"}) + "\n")
    # A real, attributed claim is unaffected by --strict.
    assert _lookup(at_path, "--mode", "owned", "--session", "session-Q", "--strict") == "T-14"
    assert _lookup(at_path, "--mode", "owned", "--session", "someone-else", "--strict") == ""


# ---------------------------------------------------------------------------
# Timestamp is load-bearing for attribution (T-13 adversarial finding #4)
# ---------------------------------------------------------------------------
def test_session_without_timestamp_gets_no_exclusive_attribution(tmp_path: Path) -> None:
    """A JSON record naming a real "session" but carrying NO "ts" at all
    cannot have been produced by .claude/hooks/claim.sh (the only
    production writer — it always writes both together). claim_lookup.py
    must not trust it as SESSION_A's exclusive claim: it degrades to the
    same unattributed bucket as a bare legacy line, so an UNRELATED session
    resolves it identically to SESSION_A (both governed by the single-
    most-recent-line amnesty, never by real attribution) — and --strict
    refuses it for everyone, including SESSION_A itself.
    """
    at_path = tmp_path / "at"
    at_path.write_text(json.dumps({"ticket": "T-5", "session": "S-A"}) + "\n")

    assert _lookup(at_path, "--mode", "last") == "T-5"
    assert _lookup(at_path, "--mode", "owned", "--session", "S-A") == "T-5"
    assert _lookup(at_path, "--mode", "owned", "--session", "totally-unrelated") == "T-5"
    assert _lookup(at_path, "--mode", "owned", "--session", "S-A", "--strict") == ""
    assert _lookup(at_path, "--mode", "owned", "--session", "totally-unrelated", "--strict") == ""


def test_session_without_timestamp_is_shadowed_once_a_newer_line_is_appended(
    tmp_path: Path,
) -> None:
    """A ts-less record naming a session is unattributed (see
    test_session_without_timestamp_gets_no_exclusive_attribution) and so is
    governed by the SAME single-most-recent-line amnesty as a bare legacy
    line: once something newer and real is appended after it, it stops
    being resolvable via --mode owned at all — not even by the session its
    own "session" field names. Meanwhile the newer, properly-attributed
    line resolves normally for its own real owner.
    """
    at_path = tmp_path / "at"
    at_path.write_text(json.dumps({"ticket": "T-2", "session": "session-B"}) + "\n")  # no ts
    with at_path.open("a") as f:
        f.write(json.dumps({"ticket": "T-1", "session": "session-A", "ts": "t2"}) + "\n")

    assert _lookup(at_path, "--mode", "owned", "--session", "session-A") == "T-1"
    assert _lookup(at_path, "--mode", "owned", "--session", "session-B") == ""


# ---------------------------------------------------------------------------
# --mode append-check (T-13 acceptance 3; adversarial findings #2/#3)
# ---------------------------------------------------------------------------
def _append_check(claims_file: Path, payload: dict) -> str:
    result = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(claims_file), "--mode", "append-check"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    return result.stdout


def test_append_check_write_that_only_appends_is_ok(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-1\n")
    payload = {"tool_name": "Write", "tool_input": {"content": "T-1\nT-2\n"}}
    assert _append_check(at_path, payload) == "ok"


def test_append_check_write_that_truncates_is_a_violation(tmp_path: Path) -> None:
    """This is finding #2/#3's exact sabotage: a Write whose content is
    just a fresh single line, exactly what CLAUDE.md's stale
    "write its ticket ID as the only line" instruction produces.
    """
    at_path = tmp_path / "at"
    at_path.write_text(json.dumps({"ticket": "T-5", "session": "sess-AAAA", "ts": "t1"}) + "\n")
    payload = {"tool_name": "Write", "tool_input": {"content": "T-14\n"}}
    assert _append_check(at_path, payload) == "violate"


def test_append_check_write_of_first_ever_claim_is_ok(tmp_path: Path) -> None:
    at_path = tmp_path / "does-not-exist-yet"
    payload = {"tool_name": "Write", "tool_input": {"content": "T-1\n"}}
    assert _append_check(at_path, payload) == "ok"


def test_append_check_edit_that_replaces_whole_content_is_a_violation(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-5\n")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"old_string": "T-5\n", "new_string": "T-14\n"},
    }
    assert _append_check(at_path, payload) == "violate"


def test_append_check_edit_that_only_appends_is_ok(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-1\nT-2\n")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"old_string": "T-2\n", "new_string": "T-2\nT-3\n"},
    }
    assert _append_check(at_path, payload) == "ok"


def test_append_check_edit_with_no_match_is_a_no_op_and_ok(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-5\n")
    payload = {"tool_name": "Edit", "tool_input": {"old_string": "x", "new_string": "y"}}
    assert _append_check(at_path, payload) == "ok"


def test_append_check_malformed_payload_fails_closed(tmp_path: Path) -> None:
    at_path = tmp_path / "at"
    at_path.write_text("T-5\n")
    result = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(at_path), "--mode", "append-check"],
        input="not valid json{{{",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "violate"


# ---------------------------------------------------------------------------
def test_legacy_line_followed_by_a_proper_claim_disambiguates(tmp_path: Path) -> None:
    """Once ANY session appends a proper record, the ambiguity the legacy
    line carried is over for --mode last (global) — the new record wins
    outright, exactly like any other append-only "last line wins" case.
    """
    at_path = tmp_path / "at"
    at_path.write_text("T-13\n")
    with at_path.open("a") as f:
        f.write(json.dumps({"ticket": "T-14", "session": "session-Q", "ts": "t"}) + "\n")
    assert _lookup(at_path, "--mode", "last") == "T-14"


# ---------------------------------------------------------------------------
# Real repo cross-checks — read-only, never mutated (see conftest docstring)
# ---------------------------------------------------------------------------
def test_real_active_ticket_parses_cleanly_under_both_modes() -> None:
    """Concrete check behind T-13 constraint 7 ("must not strand the
    claim"): whatever the live repo's .claude/active-ticket currently
    contains resolves without error under both claim_lookup.py modes. Does
    NOT assert a specific ticket value — that changes as tickets land.
    """
    if not LIVE_ACTIVE_TICKET.exists():
        pytest.skip("no live .claude/active-ticket in this checkout")
    last = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(LIVE_ACTIVE_TICKET), "--mode", "last"],
        capture_output=True, text=True, timeout=10,
    )
    assert last.returncode == 0
    owned = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(LIVE_ACTIVE_TICKET),
         "--mode", "owned", "--session", "any-probe-session-id"],
        capture_output=True, text=True, timeout=10,
    )
    assert owned.returncode == 0
    if last.stdout:
        # Still true only while the live file's LAST line is a legacy
        # (session-less) claim — the T-13-era shape this repo shipped with.
        # Once any session appends a proper record, this stops holding and
        # is exactly the disambiguation test_legacy_line_followed_by_a_
        # proper_claim_disambiguates above already covers synthetically.
        last_line = [ln for ln in LIVE_ACTIVE_TICKET.read_text().splitlines() if ln.strip()][-1]
        try:
            json.loads(last_line)
            is_legacy = False
        except (json.JSONDecodeError, ValueError):
            is_legacy = True
        if is_legacy:
            assert owned.stdout == last.stdout


def test_active_ticket_path_is_not_gitignored() -> None:
    """T-13 acceptance 3: claim records must be tracked in git. Concretely,
    .claude/active-ticket must not be excluded by .gitignore — it may or
    may not be tracked at this exact HEAD depending on claim state, but it
    must never be UNTRACKABLE.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/active-ticket"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=10,
    )
    # git check-ignore exits 0 if the path IS ignored, 1 if it is NOT.
    assert result.returncode == 1, (
        ".claude/active-ticket must not be gitignored (T-13 acceptance 3)"
    )


def test_active_ticket_path_is_tracked_in_git() -> None:
    """T-13 adversarial finding #6: "not gitignored" is NOT the same claim
    as "tracked" — a file can be un-ignored and still untracked (e.g. after
    `git rm --cached`, or if it was simply never `git add`-ed). This is the
    genuine "TRACKED IN GIT" check acceptance 3 requires: `git ls-files
    --error-unmatch` exits 0 only for a path actually present in the
    index/HEAD, not merely un-ignored.
    """
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".claude/active-ticket"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        ".claude/active-ticket must be tracked in git (T-13 acceptance 3), "
        f"got: {result.stderr!r}"
    )
