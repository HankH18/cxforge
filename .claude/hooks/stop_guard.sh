#!/usr/bin/env bash
# Delegates to harness_lib.py's hook-stop (Stop guard; blocks stopping with an open
# claim). Protocol (T-22): ticket status is DERIVED, never stored, so no ticket boundary
# needs an agent-side Edit/Write of docs/tickets.json or docs/TASKS.md -- cmd_close
# regenerates TASKS.md itself.
#
# T-27 fix (security-relevant narrowing found while migrating v1's tests to v2;
# recorded in .claude/NEEDS_HUMAN.md under "T-27 has three real defects"): v1's stop
# guard failed CLOSED when it could not identify the session; harness_lib.py's
# cmd_hook("stop") -- out of T-27's scope to edit -- instead resolves a missing/None
# session_id to "", which can never match a real per-session claim filename, so it
# silently ALLOWS an unidentifiable session to stop even while some claim is open.
# stop_guard_prep.py (new file alongside this one, in scope) intercepts that case before
# harness_lib.py ever runs: no usable session_id BLOCKS, naming the ambiguity, unless
# Claude Code's own stop_hook_active infinite-loop guard is set, which still takes
# priority. A session with a real session_id that genuinely holds no claim is untouched
# -- harness_lib.py's own no-claim branch still allows it.
#
# Coverage limitation (T-27 acceptance 4): as with scope_guard.sh, no guard in this repo
# covers the Bash tool's own write path (T-27's non_goals) -- irrelevant to the Stop
# event this hook fires on either way, noted here for consistency.
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '{"decision":"block","reason":"stop guard infra failure: python3 not found on PATH -- the guard cannot run at all, failing closed instead of silently allowing a possibly-unfinished claim to go unchecked"}'
  exit 0
fi
exec python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/stop_guard_prep.py"
