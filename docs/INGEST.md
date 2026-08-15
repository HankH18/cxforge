# Native-Task Mirror Ingestion (optional, idempotent)

The claim ledger + receipts are the real state; native Tasks are a
visibility mirror. To (re)build the mirror:

1. Read docs/tickets.json. Skip every ticket that has a receipt in
   .claude/evidence/ (already resolved) or a task already present in the
   list (re-runs must not duplicate).
2. For each remaining ticket: TaskCreate with subject "<id>: <title>"
   (the T-xxx prefix is load-bearing — the task gate parses it) and
   description = the serialized contract. Record returned task ids.
3. Wire dependencies: TaskUpdate with addBlockedBy for each depends_on
   edge whose upstream ticket is still unresolved.
4. Confirm the unblocked set matches `claim.sh status` queue tickets whose
   dependencies all have receipts — derive it; do not assume "exactly T-0".
5. Stop and report. Do not claim anything in this turn.
