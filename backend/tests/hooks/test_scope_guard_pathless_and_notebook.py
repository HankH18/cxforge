"""T-27 acceptances 2 and 3.

Acceptance 2: a PreToolUse payload whose tool_input carries no file_path
must be DENIED unless the tool is on an explicit, commented, pathless
allowlist IN THE HOOK. harness_lib.py's cmd_hook("scope") is:

    fpath = (p.get("tool_input") or {}).get("file_path")
    if not fpath: return "", 0          # i.e. ALLOW

-- unconditional, no allowlist -- and it is out of T-27's scope to edit, so
the fix lives in .claude/hooks/scope_guard_prep.py (a new hardening layer
scope_guard.sh execs into; see its own docstring), which intercepts before
harness_lib.py ever sees the payload.

Acceptance 3: NotebookEdit is added to the settings.json matcher (see
test_settings_json_matcher_includes_notebookedit below) AND its
tool_input.notebook_path is honoured by the guard. harness_lib.py's guard
only ever reads tool_input.file_path and cannot be changed, so
scope_guard_prep.py normalises notebook_path into file_path before either
the pathless check above or harness_lib.py's own PROTECTED/scope logic
ever runs -- proven here by driving the REAL hook with real NotebookEdit
payloads (never reimplementing the normalisation logic in the test).

Every test drives the REAL scope_guard.sh as a subprocess, same as
test_scope_guard.py (see conftest.run_hook / conftest.expect).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import REAL_SCOPES, REPO_ROOT, decision, expect, make_project, run_hook

# ---------------------------------------------------------------------------
# Acceptance 2: pathless payloads deny-by-default.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "NotebookEdit", "SomeUnknownTool", ""])
def test_pathless_payload_is_denied_regardless_of_tool(project: Path, tool_name: str) -> None:
    """No claim, no path anywhere in tool_input -- must DENY, not the bare
    ALLOW harness_lib.py's cmd_hook("scope") gives a payload with no
    file_path. tool_input_override builds a payload run_hook's normal
    Edit/Write/NotebookEdit shape-building can't otherwise express: one
    with no path key of any kind.
    """
    result = run_hook(
        project,
        tool_name=tool_name,
        active_ticket=None,
        tool_input_override={"some_other_field": "no path here at all"},
    )
    assert decision(result) == "deny"


def test_pathless_deny_reason_names_the_tool_and_the_allowlist_mechanism(project: Path) -> None:
    result = run_hook(
        project, tool_name="Edit", active_ticket=None, tool_input_override={},
    )
    payload = json.loads(result.stdout)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Edit" in reason
    assert "PATHLESS_ALLOWLIST" in reason


def test_pathless_payload_is_denied_even_with_an_active_matching_claim(project: Path) -> None:
    """The pathless check is unconditional and fires BEFORE any ticket
    scope is even consulted -- holding a real, matching claim doesn't
    rescue a payload the guard can't judge in the first place.
    """
    result = run_hook(
        project,
        tool_name="Edit",
        active_ticket="T-27",
        tool_input_override={"old_string": "x", "new_string": "y"},
    )
    assert decision(result) == "deny"


def test_empty_string_file_path_is_treated_as_pathless(project: Path) -> None:
    """An explicit empty string is exactly as unjudgeable as a missing key
    -- harness_lib.py's own `if not fpath` treats them identically, and so
    does this pathless check.
    """
    result = run_hook(
        project,
        tool_name="Edit",
        active_ticket=None,
        tool_input_override={"file_path": "", "old_string": "x", "new_string": "y"},
    )
    assert decision(result) == "deny"


def test_normal_edit_and_write_payloads_are_unaffected_by_the_pathless_check(project: Path) -> None:
    """Regression guard: a well-formed Edit/Write (a real file_path) must
    still be judged by ticket scope exactly as before -- the pathless
    check must not become a blanket new denial.
    """
    expect(project, ticket=None, file_path=str(project / "README.md"), want="allow")
    expect(
        project, ticket=None, tool_name="Write",
        file_path=str(project / "README.md"), want="allow",
    )


# ---------------------------------------------------------------------------
# Acceptance 3: NotebookEdit's notebook_path is honoured by the guard.
# ---------------------------------------------------------------------------


def test_notebookedit_in_scope_is_allowed(project: Path) -> None:
    assert REAL_SCOPES["T-27"] == [
        ".claude/hooks/**", ".claude/settings.json", "backend/tests/hooks/**",
    ]
    expect(
        project, ticket="T-27", tool_name="NotebookEdit",
        notebook_path=str(project / "backend/tests/hooks/test_t27_notebook.ipynb"),
        want="allow",
    )


def test_notebookedit_out_of_scope_is_denied(project: Path) -> None:
    expect(
        project, ticket="T-27", tool_name="NotebookEdit",
        notebook_path=str(project / "backend/src/analysis.ipynb"),
        want="deny",
    )


def test_notebookedit_protected_path_denied_without_claim(project: Path) -> None:
    """A notebook under a PROTECTED glob (.claude/scripts/**) must be
    denied with no active claim, exactly as an Edit/Write to the same path
    would be -- proves normalisation happens before the PROTECTED check
    too, not only the plain ticket-scope check.
    """
    expect(
        project, ticket=None, tool_name="NotebookEdit",
        notebook_path=str(project / ".claude/scripts/scratch.ipynb"),
        want="deny",
    )


def test_notebookedit_protected_path_allowed_when_sanctioned_by_claimed_scope(
    project: Path,
) -> None:
    """T-27's own scope names .claude/hooks/** (also PROTECTED) -- a
    notebook there, under an active T-27 claim, must be allowed, mirroring
    test_scope_guard.py's Edit/Write coverage of the same yield rule.
    """
    expect(
        project, ticket="T-27", tool_name="NotebookEdit",
        notebook_path=str(project / ".claude/hooks/scratch.ipynb"),
        want="allow",
    )


def test_notebookedit_agrees_with_edit_tool_for_the_same_path(project: Path) -> None:
    """Normalisation must not change the DECISION vs. what an Edit on the
    identical path would get -- only the field the path travels in.
    """
    path = str(project / "backend/tests/hooks/test_parity.ipynb")
    edit_result = run_hook(project, path, tool_name="Edit", active_ticket="T-27")
    nb_result = run_hook(
        project, tool_name="NotebookEdit", notebook_path=path, active_ticket="T-27",
    )
    assert decision(edit_result) == decision(nb_result) == "allow"


def test_notebookedit_out_of_repo_path_is_ignored_like_edit(tmp_path: Path) -> None:
    """Parity with test_scope_guard.py's
    test_out_of_repo_absolute_path_is_ignored, now via notebook_path.
    """
    tickets = {"tickets": [{"id": "T-X", "scope": ["nope/**"]}]}
    proj = make_project(tmp_path, tickets)
    outside = tmp_path.parent / "definitely-outside-the-repo.ipynb"
    result = run_hook(
        proj, tool_name="NotebookEdit", notebook_path=str(outside), active_ticket=None,
    )
    assert decision(result) == "allow"


def test_notebookedit_with_no_notebook_path_is_pathless_denied(project: Path) -> None:
    """A NotebookEdit payload missing notebook_path (and file_path) has
    nothing to normalise -- falls through to the same pathless deny as any
    other tool.
    """
    result = run_hook(
        project, tool_name="NotebookEdit", active_ticket=None,
        tool_input_override={"new_source": "print(1)"},
    )
    assert decision(result) == "deny"


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
