#!/usr/bin/env bash
# claim.sh — the ONLY production writer for T-13's append-only,
# session-scoped ticket-claim log at .claude/active-ticket. See
# claim_lookup.py for the exact record format and how each hook interprets
# it.
#
# WHY THIS EXISTS (T-13 adversarial finding #1): before this script, NOTHING
# shipped by T-13 ever wrote a session-scoped claim record — the only
# documented claiming procedure was CLAUDE.md's build-protocol step 2
# ("write its ticket ID... as the only line of .claude/active-ticket"),
# which produces a bare, session-less legacy line and, followed a second
# time, is a full-file OVERWRITE. scope_guard.sh's .claude/active-ticket
# path now REQUIRES every Edit/Write there to be a pure append (existing
# content preserved as an exact prefix — see scope_guard.sh's own header
# and claim_lookup.py's --mode append-check); a literal overwrite through
# the Edit/Write tools is denied. This script is the replacement: run it
# via the BASH tool (not Edit/Write — like every route outside the
# Edit|Write matcher, its own writes are invisible to scope_guard.sh, which
# is exactly why it can freely append here; see that hook's documented
# COVERAGE LIMITATION) to append one well-formed
# {"ticket","session","ts"} record with a REAL session id and a REAL UTC
# timestamp, every single time.
#
# USAGE:
#   bash .claude/hooks/claim.sh <ticket-id>             # claim a ticket
#   bash .claude/hooks/claim.sh --release                # release my claim
#   bash .claude/hooks/claim.sh <ticket-id> <session-id> # explicit session
#                                                          # override (tests)
#
# SESSION ID: uses $CLAUDE_CODE_SESSION_ID unless a second argument
# supplies an explicit override. Refuses to write ANY record with no
# identifiable session at all (T-13 acceptance 1: a claim records ticket id
# + CLAUDE_SESSION_ID + UTC timestamp — all three, always). The bare
# LEGACY shape (no session, no timestamp) is a documented artifact of the
# pre-T-13 file this repo migrated from (see claim_lookup.py's MIGRATION
# section); it is never something new, production code should intentionally
# produce.
set -euo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

TICKET="${1:-}"
SESSION="${2:-${CLAUDE_CODE_SESSION_ID:-}}"

if [ -z "$TICKET" ]; then
  echo "usage: claim.sh <ticket-id>|--release [session-id]" >&2
  exit 1
fi

if [ -z "$SESSION" ]; then
  echo "claim.sh: no session id available (\$CLAUDE_CODE_SESSION_ID is unset" \
       "and no override argument was given). Refusing to write an" \
       "unattributed claim — that ambiguity is exactly what T-13 exists to" \
       "remove." >&2
  exit 1
fi

AT="${PROJECT_DIR}/.claude/active-ticket"
mkdir -p "$(dirname "$AT")"

python3 - "$AT" "$TICKET" "$SESSION" <<'PYEOF'
import datetime
import json
import sys

at_path, ticket, session = sys.argv[1], sys.argv[2], sys.argv[3]
ticket_value = None if ticket == "--release" else ticket
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
record = {"ticket": ticket_value, "session": session, "ts": ts}
with open(at_path, "a") as f:
    f.write(json.dumps(record) + "\n")
PYEOF
