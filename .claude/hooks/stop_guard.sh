#!/usr/bin/env bash
# Stop hook (T-13: session-scoped, append-only ticket claims).
#
# CONTRACT (unchanged from pre-T-13): reads the Stop payload as JSON on
# stdin; ALWAYS exits 0; a block is expressed entirely as
# {"decision":"block","reason":...} on stdout. Silence on stdout + exit 0 =
# allow. This is the Stop-event contract, not scope_guard.sh's
# hookSpecificOutput shape — do not conflate the two.
#
# SESSION SCOPING (T-13 acceptance 1 + 2): pre-T-13 this hook read the
# single mutable .claude/active-ticket and blocked ANY session's Stop if
# that one ticket lacked passing evidence — so a second session opened in
# the same working directory, which never claimed that ticket, was told to
# "finish it, revert it, or release the claim" for work that was never its
# own (the observer bug that fired on T-8, T-9 and T-11). Now this hook
# only ever blocks on THIS session's own most recent claim, resolved via
# .claude/hooks/claim_lookup.py --mode owned --strict (see that file for
# the exact claim-log format and the "owned" semantics).
#
# --strict (T-13 adversarial finding #1): an UNATTRIBUTED line (a bare
# legacy line, or any record missing a real session+timestamp pair) is
# NEVER treated as this session's claim, full stop — no amnesty, unlike
# verify_gate.sh's (non-strict) lookup. A concrete, reproduced-live-against-
# this-repo bug motivated this: with the non-strict/amnesty rule, a totally
# unrelated observer session querying against the real, still-unmigrated
# single legacy line got wrongly BLOCKED — exactly the acceptance-2 bug,
# just reintroduced through the "owned by everyone" escape hatch. A false
# Stop-block on an innocent bystander is strictly worse than this hook
# occasionally failing to nag a legitimate claimant whose own claim happens
# to be an old, unattributed line — especially since this hook is a
# reminder, not the enforcement mechanism: verify_gate.sh (below) is what
# actually blocks marking a ticket complete without a passing verify run,
# and it is UNCHANGED by this trade-off.
#
# SESSION ID SOURCE: prefer the Stop payload's own "session_id" field
# (present on every hook event per the Claude Code hooks reference —
# confirmed in T-13 recon against the docs and empirically against this
# session's own env), falling back to $CLAUDE_CODE_SESSION_ID (the harness
# exports the identical value into every subprocess). If NEITHER is
# available — expected to be unreachable in practice, both sources have
# been directly confirmed present — this hook degrades to the PRE-T-13
# GLOBAL check (claim_lookup.py --mode last: whatever the log's last line
# says, session-blind) rather than silently allowing. An unidentifiable
# session can only make this hook AS STRICT as it was before T-13, never
# more permissive: a silent allow in that branch would defeat the guard's
# entire purpose (catching genuinely unfinished work) in exactly the case
# it exists to catch.
#
# ACCEPTANCE 4 (already true pre-T-13, unchanged by T-13): once the
# resolved ticket already has a passing .claude/evidence/<id>.pass, the
# claim is not "honoured" as still-open — the work is done, so this hook
# allows the stop.
set -uo pipefail
# claim_lookup.py lives next to THIS script, not necessarily under
# CLAUDE_PROJECT_DIR — tests point CLAUDE_PROJECT_DIR at a synthetic,
# disposable project dir, so resolve relative to this script's own location.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT=$(cat)
ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
[ "$ACTIVE" = "true" ] && exit 0        # REQUIRED guard: prevents infinite loop

AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
[ -f "$AT" ] || exit 0

SESSION=$(echo "$INPUT" | jq -r '.session_id // empty')
[ -z "$SESSION" ] && SESSION="${CLAUDE_CODE_SESSION_ID:-}"

LOOKUP="$HOOK_DIR/claim_lookup.py"
if [ -n "$SESSION" ]; then
  TID=$(python3 "$LOOKUP" "$AT" --mode owned --session "$SESSION" --strict)
else
  TID=$(python3 "$LOOKUP" "$AT" --mode last)
fi

[ -z "$TID" ] && exit 0    # no open claim owned by this session: not our business

[ -f "${CLAUDE_PROJECT_DIR}/.claude/evidence/${TID}.pass" ] && exit 0

echo "{\"decision\":\"block\",\"reason\":\"Ticket ${TID} is claimed by this session but has no passing verification. Finish it (verify must pass), revert and retry, or release the claim and explain why before stopping.\"}"
exit 0
