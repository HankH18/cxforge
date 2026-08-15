# Native-Task Mirror Ingestion (optional, idempotent)

The claim ledger (`.claude/claims/`) + receipts (`.claude/evidence/`) are the
real state; native Tasks are a visibility mirror only — nothing in the
harness reads them. Bind to task list **`cxforge`** (per
`.claude/rules/harness-protocol.md`; `docs/tickets.json`'s `"project"` field
still says `othram-support-agent`, the pre-rename name — that field is
metadata, not the task list to use). To (re)build the mirror:

1. Read docs/tickets.json. Skip every ticket that has a receipt in
   .claude/evidence/ (already resolved) or a task already present in the
   list (re-runs must not duplicate).
2. For each remaining ticket: TaskCreate with subject "<id>: <title>"
   (the T-xxx prefix is load-bearing — the task gate parses it) and
   description = the serialized contract. Record returned task ids.
3. Wire dependencies: TaskUpdate with addBlockedBy for each depends_on
   edge whose upstream ticket is still unresolved.
4. Compute the ready set and confirm TaskList's unblocked set matches it.
   Ready = every ticket whose DERIVED status is `queue` (no
   `.claude/claims/*.json` names it, and it has no
   `.claude/evidence/<id>.json` receipt) AND every id in its `depends_on`
   has a receipt. Get this directly rather than re-deriving it by hand:
   `.claude/scripts/claim.sh status_board` (equivalently
   `python3 .claude/scripts/harness_lib.py status_board`; a bare
   `claim.sh` with no arguments defaults to this too) prints each
   ticket's derived status. **Ticket status is never stored on the ticket
   itself** — `docs/tickets.json`'s own `status` field is dead (kept for
   plan-hygiene schema checks only; see `backend/tests/plan/
   test_status_field.py`) and nothing derives readiness from it or from
   the ad-hoc `priority` field a few tickets carry (a hand-typed batch
   label, not consulted by any script). Do not pin the confirmation to a
   single fixed root ticket — most of the graph has receipts by now;
   derive the live ready set fresh every time this doc is followed.
5. Stop and report. Do not claim anything in this turn.
