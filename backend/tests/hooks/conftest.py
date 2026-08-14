"""Shared fixtures for driving the REAL .claude/hooks/*.sh guards as
subprocesses, exactly the way Claude Code's hook machinery does.

Every test in this package uses an isolated tmp_path as CLAUDE_PROJECT_DIR —
none of them ever read or write the real repo's .claude/active-ticket (which
holds this session's own live claim). docs/tickets.json is copied from the
real repo at test time, not hand-duplicated, so the suite tracks the plan
file rather than a stale snapshot of it.

Three hook contracts are exercised here (see each hook's own header for the
authoritative version of this):
  * scope_guard.sh  — PreToolUse[Edit|Write]. ALWAYS exits 0; a deny is
    JSON on stdout (hookSpecificOutput.permissionDecision). Helpers:
    run_hook / decision / expect.
  * stop_guard.sh   — Stop. ALWAYS exits 0; a block is JSON on stdout
    ({"decision":"block",...}), differently shaped from scope_guard.sh's
    JSON. Helpers: run_stop_hook / stop_decision.
  * verify_gate.sh  — TaskCompleted, and PreToolUse[TaskUpdate]. Signals
    via EXIT CODE (0 = allow, 2 = block), stderr carries the reason — the
    opposite convention from the other two. Helpers: run_verify_hook /
    verify_decision.

T-13 (session-scoped, append-only ticket claims) changed .claude/active-
ticket from a single mutable line to an append-only JSONL claim log — see
.claude/hooks/claim_lookup.py for the exact format. ``write_claim`` below
appends one well-formed record; conftest's existing ``run_hook``/``expect``
(scope_guard.sh) keep writing a single plain string via ``active_ticket``,
which remains valid as a LEGACY claim line (see claim_lookup.py) and is
exactly the pre-T-13 shape every one of those 113 existing tests uses — none
of them needed to change.
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
STOP_HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "stop_guard.sh"
VERIFY_HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "verify_gate.sh"
CLAIM_LOOKUP_PATH = REPO_ROOT / ".claude" / "hooks" / "claim_lookup.py"
CLAIM_SH_PATH = REPO_ROOT / ".claude" / "hooks" / "claim.sh"
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


def make_project(tmp_path: Path, tickets_doc: dict[str, Any]) -> Path:
    """Build a disposable CLAUDE_PROJECT_DIR around a SYNTHETIC tickets.json.

    Unlike the ``project`` fixture (which seeds the real docs/tickets.json),
    this lets a test construct a scope shape the real plan does not happen
    to declare today — e.g. a bare single '*' wildcard, which no real
    ticket uses — without touching or duplicating the real plan file. Still
    drives the real hook script exactly like ``run_hook``/``expect`` do.
    """
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "tickets.json").write_text(json.dumps(tickets_doc))
    return tmp_path


class _Unset:
    """Sentinel type distinguishing 'leave .claude/active-ticket file as-is'
    from an explicit ``None`` (delete it) or a ``str`` (overwrite it).

    A plain ``object()`` sentinel typed as ``... | object`` collapses to
    ``object`` under mypy (``object`` is already a supertype of ``str`` and
    ``None``), which is why ``write_text`` used to fail to type-check even
    after both other branches were excluded: mypy could not narrow ``object``
    down to ``str``. A dedicated class narrows correctly via ``isinstance``.
    """


UNSET = _Unset()


def run_hook(
    project_dir: Path,
    file_path: str,
    *,
    tool_name: str = "Edit",
    active_ticket: str | None | _Unset = UNSET,
    env_extra: dict[str, str] | None = None,
    write_content: str | _Unset = UNSET,
    old_string: str = "x",
    new_string: str = "y",
    replace_all: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real scope_guard.sh hook with a synthetic PreToolUse event.

    ``active_ticket``:
      - ``UNSET`` (default): leave .claude/active-ticket in project_dir
        as-is (absent unless a previous call in the same test wrote it).
      - ``None``: delete .claude/active-ticket if present (missing-file case).
      - a string: overwrite .claude/active-ticket with that exact content,
        unmodified — "" tests empty, "   \\n" tests whitespace-only, "T-1" a
        normal claim, "T-99" an id absent from tickets.json.

    ``write_content`` (Write tool only): the literal ``tool_input.content``
    to send. ``UNSET`` (default) preserves the original fixed
    "synthetic-content" every pre-T-13 test relies on — every existing
    caller is unaffected. ``old_string``/``new_string``/``replace_all``
    (Edit tool only) default to the original fixed placeholders ("x"/"y")
    for the same reason; a caller exercising the T-13 append-only check
    against .claude/active-ticket passes real values.
    """
    at_path = project_dir / ".claude" / "active-ticket"
    if active_ticket is None:
        at_path.unlink(missing_ok=True)
    elif not isinstance(active_ticket, _Unset):
        at_path.parent.mkdir(parents=True, exist_ok=True)
        at_path.write_text(active_ticket)

    tool_input: dict[str, Any]
    if tool_name == "Write":
        content = "synthetic-content" if isinstance(write_content, _Unset) else write_content
        tool_input = {"file_path": file_path, "content": content}
    else:
        tool_input = {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        }
        if replace_all:
            tool_input["replace_all"] = True

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
    ticket: str | None | _Unset,
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


# ---------------------------------------------------------------------------
# T-13: claim-log helpers, shared by test_claim_format.py, test_stop_guard.py
# and test_verify_gate.py.
# ---------------------------------------------------------------------------
def write_claim(
    project_dir: Path,
    ticket: str | None,
    session: str | None,
    ts: str = "2026-08-14T00:00:00Z",
) -> None:
    """Append ONE well-formed claim record to project_dir/.claude/active-ticket.

    Never truncates or rewrites — opens in append mode, exactly the
    append-only contract claim_lookup.py documents. Call it more than once
    in a test to build up a multi-line log.
    """
    at_path = project_dir / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ticket": ticket, "session": session, "ts": ts}
    with at_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def run_claim_sh(
    project_dir: Path,
    ticket: str,
    *,
    session_id: str | None | _Unset = UNSET,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the REAL .claude/hooks/claim.sh writer as a subprocess.

    ``session_id``:
      - ``UNSET`` (default): pass no explicit override argument; claim.sh
        falls back to $CLAUDE_CODE_SESSION_ID as inherited from the real
        environment, UNLESS overridden via ``env_extra``.
      - ``None``: strip CLAUDE_CODE_SESSION_ID from the subprocess env AND
        pass no override argument — simulates "no session identifiable at
        all".
      - a string: passed as claim.sh's explicit second argument.
    """
    args = ["bash", str(CLAIM_SH_PATH), ticket]
    if isinstance(session_id, str):
        args.append(session_id)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    if LIVE_ACTIVE_TICKET.exists():
        assert LIVE_ACTIVE_TICKET.read_text() == _live_snapshot(), (
            "a hook test mutated the real repo's .claude/active-ticket"
        )
    return result


def run_stop_hook(
    project_dir: Path,
    *,
    stop_hook_active: bool = False,
    session_id: str | None | _Unset = UNSET,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real stop_guard.sh with a synthetic Stop event payload.

    ``session_id``:
      - ``UNSET`` (default): omit the field from the payload entirely, and
        leave $CLAUDE_CODE_SESSION_ID as inherited from the real
        environment (the value every subprocess actually gets from the
        harness) UNLESS overridden via ``env_extra``.
      - ``None``: include the payload field as JSON null (jq's `// empty`
        treats this the same as absent) AND strip
        CLAUDE_CODE_SESSION_ID from the subprocess env — simulates "neither
        source available".
      - a string: the payload's session_id field.
    """
    payload: dict[str, Any] = {
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    }
    if not isinstance(session_id, _Unset):
        payload["session_id"] = session_id

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        ["bash", str(STOP_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    if LIVE_ACTIVE_TICKET.exists():
        assert LIVE_ACTIVE_TICKET.read_text() == _live_snapshot(), (
            "a hook test mutated the real repo's .claude/active-ticket"
        )
    return result


def stop_decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a stop_guard.sh invocation to "allow" or "block".

    stop_guard.sh's contract (see its header): ALWAYS exit 0; a block is a
    top-level {"decision":"block","reason":...} object on stdout (NOT
    scope_guard.sh's hookSpecificOutput shape); silence is allow.
    """
    assert result.returncode == 0, (
        f"stop_guard.sh must always exit 0; got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return "allow"
    payload = json.loads(stdout)
    decided = payload["decision"]
    assert decided == "block", f"unexpected decision: {decided!r}"
    reason = payload.get("reason", "")
    assert reason and isinstance(reason, str), "a block must carry a human-readable reason"
    return "block"


def run_verify_hook(
    project_dir: Path,
    *,
    shape: str = "task_completed",
    subject: str | None = None,
    session_id: str | None | _Unset = UNSET,
    status: str = "completed",
    tool_name: str = "TaskUpdate",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real verify_gate.sh with a synthetic event payload.

    ``shape``:
      - "task_completed": the TaskCompleted event shape (top-level
        task_subject).
      - "pretooluse": PreToolUse[TaskUpdate] shape (tool_name/tool_input).
    ``subject``: the task subject text (may or may not contain a "T-<n>");
      ``None`` omits the field entirely (the pure status-change case that
      forces the .claude/active-ticket fallback).
    ``session_id``: same three-shape contract as run_stop_hook's parameter.
    """
    payload: dict[str, Any] = {}
    if shape == "task_completed":
        payload["hook_event_name"] = "TaskCompleted"
        if subject is not None:
            payload["task_subject"] = subject
    elif shape == "pretooluse":
        payload["hook_event_name"] = "PreToolUse"
        payload["tool_name"] = tool_name
        tool_input: dict[str, Any] = {"status": status}
        if subject is not None:
            tool_input["subject"] = subject
        payload["tool_input"] = tool_input
    else:
        raise ValueError(f"unknown shape: {shape!r}")

    if not isinstance(session_id, _Unset):
        payload["session_id"] = session_id

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        ["bash", str(VERIFY_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    if LIVE_ACTIVE_TICKET.exists():
        assert LIVE_ACTIVE_TICKET.read_text() == _live_snapshot(), (
            "a hook test mutated the real repo's .claude/active-ticket"
        )
    return result


def verify_decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a verify_gate.sh invocation to "allow" or "block".

    verify_gate.sh's contract (see its header): signals via EXIT CODE, not
    JSON — 0 is allow, 2 is block, stderr carries the human-readable
    reason on a block. Any other exit code is a bug, not a valid decision.
    """
    if result.returncode == 0:
        return "allow"
    assert result.returncode == 2, (
        f"verify_gate.sh must exit 0 (allow) or 2 (block); got "
        f"{result.returncode}. stderr={result.stderr!r}"
    )
    assert result.stderr.strip(), "a block must carry a human-readable stderr reason"
    return "block"
