"""T-27: the scope guard fails closed on every input it cannot judge.

Three real gaps survived the v1 -> v2 migration, all in the PreToolUse write
path. Each is fixed in the HOOK layer (`.claude/hooks/scope_guard.sh` and its
`scope_guard_prep.py` helper), not in `harness_lib.py` — that is T-27's own
scope, and acceptance 2 says so explicitly ("an explicit commented pathless
allowlist **in the hook**").

  1. `cmd_hook("scope")` read `tool_input.file_path` and, finding none,
     returned ("", 0) — a silent ALLOW. Any Edit/Write-matched payload without
     that key went entirely unjudged.
  2. `.claude/settings.json`'s matcher was `Edit|Write`, so `NotebookEdit`
     never reached the guard at all: notebook writes bypassed scope control
     completely. The tool carries `notebook_path`, not `file_path`.
  3. Infra failure (the python3 helper missing or erroring) produced no
     decision rather than a deny.

Every test here drives the REAL hook through a synthetic project, exactly as
Claude Code invokes it — no mirrored logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import REPO_ROOT, decision, run_hook

# The synthetic project's tickets.json (see conftest.make_project) gives this
# ticket a scope covering backend/**; anything outside it must be denied.
IN_SCOPE = "backend/src/data/db.py"
OUT_OF_SCOPE = "portal/src/App.tsx"


# ---------------------------------------------------------------------------
# Gap 1 — a payload with no path at all
# ---------------------------------------------------------------------------
def test_edit_payload_with_no_file_path_is_denied(project: Path) -> None:
    """The core of acceptance 2. An Edit payload carrying no `file_path` key
    used to be allowed silently, so anything able to shape such a payload
    escaped scope control entirely. It must now be denied."""
    result = run_hook(
        project,
        active_ticket="T-1",
        tool_input_override={"old_string": "x", "new_string": "y"},
    )
    assert decision(result) == "deny"


def test_edit_payload_with_an_empty_file_path_is_denied(project: Path) -> None:
    """Present-but-empty is the same unjudgeable case as absent — an empty
    string names no file, so there is nothing to check a scope against."""
    result = run_hook(
        project,
        active_ticket="T-1",
        tool_input_override={"file_path": "", "old_string": "x", "new_string": "y"},
    )
    assert decision(result) == "deny"


def test_payload_with_no_tool_input_at_all_is_denied(project: Path) -> None:
    """Fail closed on a malformed payload rather than treating a missing
    `tool_input` as 'nothing to guard'."""
    result = run_hook(project, active_ticket="T-1", tool_input_override={})
    assert decision(result) == "deny"


def test_the_pathless_deny_is_not_a_blanket_deny(project: Path) -> None:
    """Guards the obvious over-correction: denying pathless payloads must not
    also deny ordinary in-scope writes. Without this, 'fail closed' could be
    satisfied by a guard that denies everything, which would be useless."""
    assert decision(run_hook(project, str(project / IN_SCOPE), active_ticket="T-1")) == "allow"


# ---------------------------------------------------------------------------
# Gap 2 — NotebookEdit
# ---------------------------------------------------------------------------
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


def test_notebook_edit_out_of_scope_is_denied(project: Path) -> None:
    """Acceptance 3, second half: `notebook_path` is honoured. A notebook
    outside the claimed ticket's scope must be denied exactly as an ordinary
    file would be."""
    result = run_hook(
        project,
        tool_name="NotebookEdit",
        notebook_path=str(project / "portal" / "analysis.ipynb"),
        active_ticket="T-1",
    )
    assert decision(result) == "deny"


def test_notebook_edit_in_scope_is_allowed(project: Path) -> None:
    """The paired allow, so the deny above cannot pass by rejecting every
    NotebookEdit payload regardless of path."""
    result = run_hook(
        project,
        tool_name="NotebookEdit",
        notebook_path=str(project / "backend" / "src" / "data" / "explore.ipynb"),
        active_ticket="T-1",
    )
    assert decision(result) == "allow"


def test_notebook_edit_with_no_notebook_path_is_denied(project: Path) -> None:
    """A NotebookEdit that names no notebook is as unjudgeable as a pathless
    Edit, and must fail closed the same way."""
    result = run_hook(
        project,
        tool_name="NotebookEdit",
        tool_input_override={"new_source": "print(1)", "cell_type": "code"},
        active_ticket="T-1",
    )
    assert decision(result) == "deny"


def test_notebook_edit_cannot_reach_protected_paths(project: Path) -> None:
    """The bypass mattered because it reached protected state, not just
    out-of-scope product files: before the matcher fix, a notebook write to
    .claude/evidence/ was never checked at all."""
    result = run_hook(
        project,
        tool_name="NotebookEdit",
        notebook_path=str(project / ".claude" / "evidence" / "forged.ipynb"),
        active_ticket="T-1",
    )
    assert decision(result) == "deny"


# ---------------------------------------------------------------------------
# Gap 3 — infrastructure failure
# ---------------------------------------------------------------------------
def test_guard_denies_when_python3_is_unavailable(project: Path) -> None:
    """Acceptance 1, simulated via PATH manipulation exactly as the ticket
    specifies. If the helper cannot run, the guard must produce a deny that
    names the infra failure — never a silent allow, which is what an
    exit-non-zero-with-no-decision amounts to at the PreToolUse layer."""
    # /bin carries bash but not python3, so the hook itself still launches and
    # gets the chance to notice the helper is gone. Emptying PATH entirely would
    # only prove that a hook which cannot start produces no decision, which is a
    # different (and untestable-from-here) failure.
    result = run_hook(
        project,
        str(project / IN_SCOPE),
        active_ticket="T-1",
        env_extra={"PATH": "/bin"},
    )
    assert decision(result) == "deny", (
        f"guard did not fail closed with python3 unavailable: "
        f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "infra failure" in result.stdout, (
        "the deny must NAME the infrastructure failure, per acceptance 1 — an "
        f"unexplained deny is not the same thing. got: {result.stdout!r}"
    )


def test_the_infra_deny_is_specific_to_the_failure(project: Path) -> None:
    """Paired control for the test above: with python3 present, the very same
    in-scope write is allowed. Without this, an unconditional deny would
    satisfy the PATH test while breaking the guard entirely."""
    assert decision(run_hook(project, str(project / IN_SCOPE), active_ticket="T-1")) == "allow"
