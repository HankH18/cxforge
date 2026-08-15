#!/usr/bin/env python3
"""T-27 hardening layer in front of harness_lib.py's hook-scope.

Lives in .claude/hooks/ (NOT harness_lib.py, which is out of T-27's scope)
per T-27 acceptance 2's "explicit commented pathless allowlist in the
hook". scope_guard.sh execs into this after confirming python3 itself is
on PATH (that check has to live one layer up in bash -- if python3 is
entirely missing, this script can never run to handle it).

Three of T-27's fixes live here:
  1. Pathless-payload deny-by-default (acceptance 2): harness_lib.py's
     cmd_hook("scope") returns ALLOW outright when tool_input has no
     file_path -- no allowlist, no deny. This script intercepts before
     harness_lib.py ever sees the payload.
  2. NotebookEdit's notebook_path normalised into file_path (acceptance
     3): harness_lib.py's guard only ever reads tool_input.file_path and
     cannot be edited from here, so normalising is the only way to make
     NotebookEdit payloads judgeable at all.
  3. Fail-closed if delegating to harness_lib.py itself errors
     (acceptance 1's "or erroring" half; the "python3 absent" half is
     handled one layer up in scope_guard.sh, see above).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# Tools that may legitimately reach this guard (the PreToolUse matcher in
# .claude/settings.json is "Edit|Write|NotebookEdit") carrying NO path at
# all in tool_input. Deliberately empty: Edit and Write always carry
# file_path, and NotebookEdit's notebook_path is normalised into file_path
# below before this allowlist is even consulted -- so no tool currently
# reachable through the matcher is legitimately pathless. Add an entry
# here ONLY alongside a comment justifying why that specific tool
# legitimately carries no path; anything else without a path is denied.
PATHLESS_ALLOWLIST: set[str] = set()


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _deny(
            "scope guard infra failure: could not parse the PreToolUse payload "
            f"({e}) -- failing closed rather than silently allowing"
        )
        return

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    payload["tool_input"] = tool_input

    # Fix 2: normalise NotebookEdit's notebook_path into file_path before
    # anything downstream (the pathless check right below, or
    # harness_lib.py's guard) ever looks for a path.
    if (
        tool_name == "NotebookEdit"
        and not tool_input.get("file_path")
        and tool_input.get("notebook_path")
    ):
        tool_input["file_path"] = tool_input["notebook_path"]

    # Fix 1: deny-by-default when no path survived normalisation.
    if not tool_input.get("file_path"):
        if tool_name not in PATHLESS_ALLOWLIST:
            _deny(
                f"pathless {tool_name or '<unknown tool>'} payload denied: tool_input "
                "carries no file_path (and, for NotebookEdit, no notebook_path either), "
                "so the scope guard cannot judge it against ticket scope. Only a tool "
                "explicitly listed in scope_guard_prep.py's PATHLESS_ALLOWLIST, with a "
                "comment justifying it, may bypass this."
            )
            return
        # else: explicitly sanctioned above; fall through so harness_lib.py's
        # own fpath-missing branch allows it, exactly as it does today.

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    lib = os.path.join(project_dir, ".claude", "scripts", "harness_lib.py")
    try:
        result = subprocess.run(
            [sys.executable, lib, "hook-scope"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as e:
        # Fix 3 (the "erroring" half of acceptance 1): harness_lib.py itself
        # could not even be launched (missing file, exec failure, timeout...).
        _deny(
            f"scope guard infra failure: could not run harness_lib.py hook-scope ({e}) "
            "-- failing closed rather than silently allowing"
        )
        return

    if result.returncode != 0:
        # Fix 3: hook-scope is contractually supposed to ALWAYS exit 0 (a
        # deny is expressed in JSON on stdout, never via exit code -- see
        # harness_lib.py's cmd_hook and conftest.py's decision()). A nonzero
        # exit means it crashed before producing a decision at all.
        _deny(
            f"scope guard infra failure: harness_lib.py hook-scope exited "
            f"{result.returncode} instead of producing a decision -- failing closed "
            f"rather than silently allowing (stderr: {result.stderr.strip()[:300]})"
        )
        return

    out = result.stdout
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    # empty stdout == allow; print nothing (default), exit 0.


if __name__ == "__main__":
    main()
