"""Shared fixtures for driving the REAL .claude/hooks/scope_guard.sh as a
subprocess, exactly the way Claude Code's PreToolUse machinery does: JSON on
stdin, a decision expressed in JSON on stdout, exit code always 0.

Every test in this package uses an isolated tmp_path as CLAUDE_PROJECT_DIR —
none of them ever read or write the real repo's .claude/active-ticket (which
holds this session's own live claim). docs/tickets.json is copied from the
real repo at test time, not hand-duplicated, so the suite tracks the plan
file rather than a stale snapshot of it.
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
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "scope_guard.sh"
REAL_TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"

# Read once, read-only: every ticket id + scope list the real plan declares
# today. Tests derive coverage checks from this rather than a hand-copied
# constant, so a ticket added to the plan later shows up here automatically.
REAL_TICKETS: dict[str, Any] = json.loads(REAL_TICKETS_PATH.read_text())
REAL_TICKET_IDS: list[str] = [t["id"] for t in REAL_TICKETS["tickets"]]
REAL_SCOPES: dict[str, list[str]] = {
    t["id"]: t["scope"] for t in REAL_TICKETS["tickets"]
}

# Not used to bypass anything — just proves this file never mutates the
# session's own live claim.
LIVE_ACTIVE_TICKET = REPO_ROOT / ".claude" / "active-ticket"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A disposable CLAUDE_PROJECT_DIR seeded with the real docs/tickets.json.

    No .claude/active-ticket exists yet — individual tests create/overwrite
    it via ``run_hook(..., active_ticket=...)``.
    """
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_TICKETS_PATH, tmp_path / "docs" / "tickets.json")
    return tmp_path


def run_hook(
    project_dir: Path,
    file_path: str,
    *,
    tool_name: str = "Edit",
    active_ticket: str | None | object = ...,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real scope_guard.sh hook with a synthetic PreToolUse event.

    ``active_ticket``:
      - ``...`` (default): leave .claude/active-ticket in project_dir as-is
        (absent unless a previous call in the same test wrote it).
      - ``None``: delete .claude/active-ticket if present (missing-file case).
      - a string: overwrite .claude/active-ticket with that exact content,
        unmodified — "" tests empty, "   \\n" tests whitespace-only, "T-1" a
        normal claim, "T-99" an id absent from tickets.json.
    """
    at_path = project_dir / ".claude" / "active-ticket"
    if active_ticket is None:
        at_path.unlink(missing_ok=True)
    elif active_ticket is not ...:
        at_path.parent.mkdir(parents=True, exist_ok=True)
        at_path.write_text(active_ticket)

    if tool_name == "Write":
        tool_input = {"file_path": file_path, "content": "synthetic-content"}
    else:
        tool_input = {"file_path": file_path, "old_string": "x", "new_string": "y"}

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    # The session's own claim must never move, no matter what a test does.
    if LIVE_ACTIVE_TICKET.exists():
        assert LIVE_ACTIVE_TICKET.read_text() == _live_snapshot(), (
            "a hook test mutated the real repo's .claude/active-ticket"
        )
    return result


_LIVE_SNAPSHOT_CACHE: str | None = None


def _live_snapshot() -> str:
    global _LIVE_SNAPSHOT_CACHE
    if _LIVE_SNAPSHOT_CACHE is None:
        _LIVE_SNAPSHOT_CACHE = LIVE_ACTIVE_TICKET.read_text()
    return _LIVE_SNAPSHOT_CACHE


def decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a hook invocation to "allow" or "deny".

    The hook's contract (see its header) is: ALWAYS exit 0; a deny is a JSON
    object on stdout, an allow is silence. Anything else is a bug in the
    hook or in the test, not a valid decision, so it is not swallowed.
    """
    assert result.returncode == 0, (
        f"scope_guard.sh must always exit 0 (deny is expressed in JSON, not "
        f"exit code); got {result.returncode}. stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return "allow"
    payload = json.loads(stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    decided = output["permissionDecision"]
    assert decided == "deny", f"unexpected permissionDecision: {decided!r}"
    reason = output.get("permissionDecisionReason", "")
    assert reason and isinstance(reason, str), "a deny must carry a human-readable reason"
    return "deny"


def expect(
    project_dir: Path,
    *,
    ticket: str | None | object,
    file_path: str,
    want: str,
    tool_name: str = "Edit",
    env_extra: dict[str, str] | None = None,
) -> None:
    """Assert the hook's decision for one (ticket claim, path) pair.

    ``ticket`` is forwarded to ``run_hook``'s ``active_ticket`` verbatim, so
    it accepts the same three shapes (leave-as-is / delete / overwrite).
    """
    result = run_hook(
        project_dir,
        file_path,
        tool_name=tool_name,
        active_ticket=ticket,
        env_extra=env_extra,
    )
    got = decision(result)
    assert got == want, (
        f"ticket={ticket!r} path={file_path!r} tool={tool_name}: "
        f"expected {want}, got {got} (stdout={result.stdout!r})"
    )
