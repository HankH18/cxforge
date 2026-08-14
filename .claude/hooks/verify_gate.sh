#!/usr/bin/env bash
# Ticket completion gate.
#
# Wired to TWO events (see .claude/settings.json):
#   * TaskCompleted  — fires when a task is marked completed.
#   * PreToolUse[TaskUpdate] — belt and braces, and the one that is
#     verifiable from inside a session.
#
# Exit 2 blocks; stderr becomes the reason the model sees.
#
# HISTORY: the original version read the subject from `.task.subject`. The
# real TaskCompleted payload carries a TOP-LEVEL `task_subject`, so the jq
# expression returned empty, TID was empty, and the script took its
# "not a ticket task; allow" branch on every single call. The gate fired
# and rubber-stamped everything. Hence the defensive multi-shape parsing
# and the explicit no-silent-allow rule below.
#
# SESSION SCOPING (T-13: session-scoped, append-only ticket claims;
# acceptance 1 + 2): this hook now acts ONLY on a ticket the CURRENT
# session has itself claimed, resolved via .claude/hooks/claim_lookup.py
# (the same mechanism stop_guard.sh uses — scope_guard.sh deliberately
# stays global; see its own header for why). A ticket id can arrive two
# ways:
#   (a) EXPLICIT — a subject that names a ticket directly (task_subject /
#       task.subject / subject / tool_input.subject). Accepted only if it
#       equals claim_lookup.py's `--mode owned` answer for THIS session;
#       otherwise this hook is not this event's business (exit 0, TID
#       cleared before the "no ticket identifiable" check below). A
#       session cannot verify-gate or stamp evidence for a ticket it never
#       claimed — the direct fix, applied to this hook, of the
#       acceptance-2 "observer" bug (a second session's own unrelated
#       TaskUpdate must never run, or write evidence for, a DIFFERENT
#       session's ticket just because its subject happens to contain a
#       "T-<n>"-shaped substring).
#   (b) FALLBACK — a pure status-change TaskUpdate carries no subject at
#       all. Pre-T-13 this read the single global .claude/active-ticket
#       unconditionally; now it reads THIS session's own current claim via
#       claim_lookup.py `--mode owned`, for the identical reason as (a).
# SESSION ID SOURCE / DEGRADE: identical rule to stop_guard.sh — prefer the
# payload's own "session_id", else $CLAUDE_CODE_SESSION_ID. If NEITHER is
# available (expected unreachable — both sources confirmed present during
# T-13 recon), this hook degrades to the exact PRE-T-13 unscoped behaviour
# for BOTH sources above: an explicit subject's ticket id is used directly
# with no ownership cross-check, and the fallback reads claim_lookup.py
# `--mode last` (the log's last line, session-blind, same shape as the old
# `head -n1 .claude/active-ticket`). This can only be AS STRICT as before
# T-13, never a silent allow that defeats the gate.
#
# OWNERSHIP LOOKUP MODE — deliberately NOT --strict (contrast
# stop_guard.sh): both the explicit-subject cross-check and the no-subject
# fallback below call claim_lookup.py's --mode owned WITHOUT --strict, so
# an unattributed claim-log line is granted "owned by whoever asks" amnesty
# when — and only when — it is the single most recent line in the ENTIRE
# log (see claim_lookup.py's resolve_owned() and its finding-#5 fix: a
# newer record appended after it, attributed or not, ends the amnesty).
# This is a DELIBERATE, narrower fix than stop_guard.sh's: making this
# hook's ownership check --strict would ALSO make it fail-open (allow
# without ever running verify) the moment a session's real id can't be
# proven to match an old, unattributed claim — including the CURRENT live
# claim this exact repo shipped T-13 with. That is a silent disable of
# "a ticket is done only when its verify command exits 0" for real,
# in-flight work, which is a strictly worse failure mode than
# stop_guard.sh's occasional false nag, and is exactly the kind of
# rubber-stamping this hook's own HISTORY note above was written to
# prevent. Net effect: a THIRD, unrelated session's TaskUpdate can no
# longer be misattributed to a stale claim once a newer claim has been
# recorded (finding #5, fixed for both hooks via the shared resolver); a
# THIRD session's subject-less TaskUpdate racing against the repo's single,
# still-unmigrated legacy line (finding #1's broader, unproven-in-practice
# claim about this hook) remains a narrow, bounded, migration-only
# exposure — closed going forward by every FUTURE claim going through
# .claude/hooks/claim.sh, which always records a real session + timestamp
# together and so never falls into the amnesty branch at all.
#
# ACCEPTANCE 4 for THIS hook: once the resolved ticket already has a
# passing .claude/evidence/<id>.pass, this hook allows immediately without
# re-running verify. This is an idempotency short-circuit, not a
# weakening of "a ticket is done only when its verify command exits 0" —
# the evidence file only exists because `bash -c "$VERIFY"` already exited
# 0 for this exact ticket on this exact checkout (evidence lives under
# .claude/evidence/, gitignored but shared on disk by every session working
# the same directory, so it is trustworthy local proof, not a claim from
# elsewhere). This is also this hook's reading of "a claim whose ticket
# already has evidence is not honoured" (T-13 acceptance 4): an
# already-proven-done claim is not treated as still requiring a fresh gate.
set -uo pipefail
# claim_lookup.py lives next to THIS script, not necessarily under
# CLAUDE_PROJECT_DIR — tests point CLAUDE_PROJECT_DIR at a synthetic,
# disposable project dir, so resolve relative to this script's own location.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT=$(cat)

TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [ -n "$TOOL" ]; then
  # PreToolUse shape. Only gate the transition INTO completed.
  [ "$TOOL" = "TaskUpdate" ] || exit 0
  STATUS=$(echo "$INPUT" | jq -r '.tool_input.status // empty')
  [ "$STATUS" = "completed" ] || exit 0
fi

SESSION=$(echo "$INPUT" | jq -r '.session_id // empty')
[ -z "$SESSION" ] && SESSION="${CLAUDE_CODE_SESSION_ID:-}"

# Subject field name differs per event; try every documented shape.
SUBJECT=$(echo "$INPUT" | jq -r '
  .task_subject // .task.subject // .subject // .tool_input.subject // empty')
EXPLICIT_TID=$(echo "$SUBJECT" | grep -oE 'T-[0-9]+' | head -n1 || true)

AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
LOOKUP="$HOOK_DIR/claim_lookup.py"

if [ -n "$EXPLICIT_TID" ]; then
  TID="$EXPLICIT_TID"
  if [ -n "$SESSION" ]; then
    OWNED=$(python3 "$LOOKUP" "$AT" --mode owned --session "$SESSION")
    [ "$TID" != "$OWNED" ] && TID=""
  fi
  # SESSION unknown: degrade path, TID stays as EXPLICIT_TID (pre-T-13 parity).
else
  if [ -n "$SESSION" ]; then
    TID=$(python3 "$LOOKUP" "$AT" --mode owned --session "$SESSION")
  else
    TID=$(python3 "$LOOKUP" "$AT" --mode last)
  fi
fi

# No ticket identifiable, or identifiable but not this session's claim:
# either way, genuinely allowed (T-13 acceptance 1 + 2).
[ -z "$TID" ] && exit 0

# Acceptance 4: already durably proven — nothing left to gate.
[ -f "${CLAUDE_PROJECT_DIR}/.claude/evidence/${TID}.pass" ] && exit 0

TICKETS="${CLAUDE_PROJECT_DIR}/docs/tickets.json"
VERIFY=$(jq -r --arg id "$TID" \
  '.tickets[] | select(.id==$id) | .verify' "$TICKETS" 2>/dev/null)

# An unknown ticket id, or a ticket with no verify command, must NOT be a
# silent pass — that is exactly the failure mode this script already had.
if [ -z "$VERIFY" ] || [ "$VERIFY" = "null" ]; then
  echo "verify_gate: no verify command found for ${TID} in docs/tickets.json." \
       "Refusing to close a ticket that cannot be verified." >&2
  exit 2
fi

cd "$CLAUDE_PROJECT_DIR" || exit 2
if bash -c "$VERIFY"; then
  mkdir -p .claude/evidence && date -u +%s > ".claude/evidence/${TID}.pass"
  exit 0
fi

rm -f ".claude/evidence/${TID}.pass"
echo "verify failed for ${TID}: ${VERIFY} — ticket cannot be closed." \
     "Revert to the ticket-start commit and retry." >&2
exit 2
