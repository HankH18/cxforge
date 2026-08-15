"""T-26 acceptance 4: docs/INGEST.md is regenerated to match reality.

The pre-T-26 version of docs/INGEST.md's confirmation step (step 4) read
"Confirm: TaskList shows every ticket, with exactly T-0 unblocked (the
graph's sole root)." A literal follower of that instruction fails its own
confirmation step the moment more than T-0 has a receipt (true for most of
this plan's life) -- the ready set must be DERIVED from the live claim
ledger + receipts, never hardcoded to a specific ticket id or count.

It must also name the task list new sessions bind to. The ticket text (T-26
acceptance 4) says "othram-support-agent"; that is docs/tickets.json's
`"project"` field (the pre-rename project name), not the task list a
session binds to -- `.claude/rules/harness-protocol.md` states the real one
explicitly: "Task list name: cxforge". These tests hold docs/INGEST.md to
the real name and to deriving readiness live, not to hardcoding either the
stale name or a stale ready-set snapshot.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INGEST_PATH = REPO_ROOT / "docs" / "INGEST.md"
HARNESS_PROTOCOL_PATH = REPO_ROOT / ".claude" / "rules" / "harness-protocol.md"


def test_ingest_doc_does_not_hardcode_a_stale_confirmation_target() -> None:
    text = INGEST_PATH.read_text()
    assert "exactly T-0" not in text, (
        "docs/INGEST.md still asserts a fixed single-root confirmation "
        "target -- a literal follower fails this the moment any ticket "
        "beyond T-0 has a receipt (true for most of this plan's history). "
        "The ready set must be derived live, not pinned to one id/count."
    )


def test_ingest_doc_names_the_real_task_list() -> None:
    """Cross-checked against .claude/rules/harness-protocol.md's own
    statement of the task list name, rather than hand-copying the string
    twice -- so this test can't itself drift from the authoritative source
    if the name is ever legitimately changed again."""
    protocol_text = HARNESS_PROTOCOL_PATH.read_text()
    assert "Task list name: cxforge" in protocol_text, (
        "this test's authoritative source, .claude/rules/harness-protocol.md, "
        "no longer states the task list name in the expected form -- update "
        "this test (and docs/INGEST.md) to match, not silently"
    )
    ingest_text = INGEST_PATH.read_text()
    assert "cxforge" in ingest_text, (
        "docs/INGEST.md must name the task list ('cxforge', per "
        ".claude/rules/harness-protocol.md) that new sessions bind to"
    )


def test_ingest_doc_describes_derived_status_not_a_stored_field() -> None:
    """The mechanism description must match harness_lib.status(tid): a pure
    function of claim/receipt file existence, never a read of
    docs/tickets.json's own (dead) `status` field or its unused `priority`
    field -- see backend/tests/plan/test_status_field.py."""
    text = INGEST_PATH.read_text()
    assert "derived" in text.lower() or "DERIVED" in text, (
        "docs/INGEST.md must describe ticket status as derived from "
        ".claude/claims/ and .claude/evidence/, not as a stored field"
    )
    assert "status_board" in text, (
        "docs/INGEST.md should point at the real, working mechanism for "
        "getting derived status ('status_board') rather than leaving the "
        "reader to hand-derive it from raw claim/evidence files"
    )
