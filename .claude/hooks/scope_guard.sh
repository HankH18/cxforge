#!/usr/bin/env bash
# Delegates to harness_lib.py's hook-scope (PreToolUse Edit/Write/NotebookEdit guard;
# allow/deny by ticket scope). Protocol (T-22): ticket status is DERIVED, never stored,
# so no ticket boundary needs an agent-side Edit/Write of docs/tickets.json or
# docs/TASKS.md -- cmd_close regenerates TASKS.md itself.
#
# T-27 hardening: this used to `exec python3 harness_lib.py hook-scope` directly, which
# meant (a) a payload with no tool_input.file_path sailed through unjudged (ALLOW), (b)
# NotebookEdit (tool_input.notebook_path, not file_path) was invisible to the guard even
# once added to the matcher below, and (c) a missing python3 made this line exit non-zero
# with a shell error and no deny decision at all -- a silent allow from Claude Code's
# point of view. harness_lib.py itself is out of T-27's scope to edit, so all three fixes
# live here and in scope_guard_prep.py (a new file alongside this one, in scope):
#   1. python3 absence is checked FIRST, right here in bash, since if python3 is
#      genuinely missing nothing written in python3 could run to handle it -- deny
#      directly, naming the infra failure, instead of falling through to `exec`.
#   2. Everything else (the pathless-payload allowlist, NotebookEdit normalisation, and
#      failing closed if harness_lib.py itself errors) lives in scope_guard_prep.py,
#      which this execs into once python3 is confirmed present.
#
# Coverage limitation (T-27 acceptance 4, narrowed from before): pathless payloads,
# NotebookEdit bypassing the guard, and a python3 infra failure producing no decision are
# now all closed. What remains OUT of coverage, by design (T-27's non_goals): the Bash
# tool's own write path (redirection, sed -i, heredocs, ...) is not matched by the
# PreToolUse hook at all -- only Edit/Write/NotebookEdit are. Integrity of protected
# plan/harness files against an out-of-band Bash write is T-26's close-time snapshot job,
# not this guard's.
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"scope guard infra failure: python3 not found on PATH -- the guard cannot run at all, failing closed instead of silently allowing"}}'
  exit 0
fi
exec python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/scope_guard_prep.py"
