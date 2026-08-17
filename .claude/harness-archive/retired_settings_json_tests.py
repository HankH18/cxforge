"""Retired 2026-08-16 by docs/DECISIONS.md ADR-019. NOT COLLECTED, NOT RUN.

These three tests were T-27 acceptance criteria. Each read the LIVE
``.claude/settings.json`` and asserted the ``PreToolUse`` scope-guard hooks were
installed. Work package W0.2 removed those hooks — an owner-approved change
implementing ADR-001, which retired the cc-factory ticket harness — so all three
began failing with ``KeyError: 'PreToolUse'``.

They are preserved, not deleted, and were NOT rewritten. The ``justify-test-edit``
gate refused the edit on two independent grounds: the discriminating question
("would this still fail if I reverted my change?") answers *no*, and acceptance
tests are escalated to the human rather than edited by an agent. The owner chose
retirement.

The other 326 tests in ``backend/tests/hooks/`` were deliberately KEPT. They
exercise the guard scripts' own logic against synthetic fixtures and do not care
whether the hooks are installed.

INVARIANT THIS NO LONGER ENFORCES, stated here for anyone reviving the harness:
``.claude/settings.json`` should carry a ``PreToolUse`` matcher covering
``Edit|Write|NotebookEdit`` (NotebookEdit uses ``notebook_path``, not
``file_path``, so it must be named explicitly), and no ``PreToolUse`` hook should
match ``Bash`` — the Bash write path was never in the guard's coverage by design.
To revive: restore the hooks in settings.json, then move each function back to the
file named in its comment below.
"""

# These names came from backend/tests/hooks/conftest.py. Redefined here so the
# archived module is self-contained and lints cleanly. It is never collected.
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"


# ---- from backend/tests/hooks/test_scope_guard_pathless_and_notebook.py ----
def test_settings_json_matcher_includes_notebookedit() -> None:
    """Acceptance 3's other half: the REAL .claude/settings.json PreToolUse
    matcher must actually route NotebookEdit calls to scope_guard.sh, or
    the shim-level normalisation above is unreachable in practice.
    """
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    matchers = [
        entry["matcher"]
        for entry in settings["hooks"]["PreToolUse"]
        if "scope_guard.sh" in entry["hooks"][0]["command"]
    ]
    assert matchers, "no PreToolUse entry routes to scope_guard.sh at all"
    assert all("NotebookEdit" in m for m in matchers)
    assert all("Edit" in m and "Write" in m for m in matchers), (
        "NotebookEdit must be ADDED to the matcher, not replace Edit|Write"
    )


# ---- from backend/tests/hooks/test_scope_guard_fail_closed.py ----
def test_notebook_edit_is_matched_by_settings_json() -> None:
    """Acceptance 3, first half. The guard cannot judge a tool the harness
    never routes to it, so the matcher itself is part of the contract.

    Asserted against the REAL `.claude/settings.json`, deliberately: the
    synthetic fixture project does not carry one, and a matcher that only
    named NotebookEdit inside a tmp_path copy would prove nothing about what
    the live harness actually routes."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    matchers = [
        h["matcher"]
        for h in settings["hooks"]["PreToolUse"]
        if "scope_guard" in json.dumps(h)
    ]
    assert matchers, "no PreToolUse entry routes to scope_guard.sh"
    assert all("NotebookEdit" in m for m in matchers), (
        f"NotebookEdit missing from the scope_guard matcher(s): {matchers} — "
        "notebook writes would bypass the scope guard entirely"
    )


# ---- from backend/tests/hooks/test_close_unattributed_claim_gap.py ----
def test_no_pretooluse_hook_matches_bash_tool_calls() -> None:
    """Pins the architectural fact the whole file's "unreachable via hooks" finding
    rests on: reads the REAL `.claude/settings.json` and asserts none of its
    `PreToolUse` entries would ever fire for a Bash tool call (the only way
    `claim.sh close` -- and so `cmd_close` -- is ever invoked, per harness-protocol.md
    rule 2). If a future change wires a `PreToolUse` hook onto `Bash`, this test starts
    failing, which is exactly the signal that a hook-layer fix for T-28 acceptance 1
    becomes possible again.
    """
    settings = json.loads(SETTINGS_JSON.read_text())
    pretooluse = settings["hooks"]["PreToolUse"]
    assert pretooluse, "expected at least one PreToolUse entry"
    for entry in pretooluse:
        matcher = entry.get("matcher", "")
        tool_names = matcher.split("|") if matcher else []
        assert "Bash" not in tool_names, (
            f"a PreToolUse hook now matches Bash ({entry!r}) -- cmd_close is reachable "
            "via a hook again; T-28 acceptance 1 may now be fixable in .claude/hooks/**"
        )
    # And the converse, so this test cannot pass vacuously on an empty/renamed file:
    # scope_guard.sh's own matcher is exactly what we expect it to be today.
    matchers = {entry.get("matcher", "") for entry in pretooluse}
    assert "Edit|Write|NotebookEdit" in matchers
    assert "TaskUpdate" in matchers
