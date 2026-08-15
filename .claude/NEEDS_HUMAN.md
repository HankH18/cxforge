# Needs human

## 2026-08-15 — audit evidence missing

The auditor cannot validate any resolved ticket: `.claude/evidence/` is empty despite the plan and commit history showing many closed tickets. Please restore the receipt artifacts (or direct an authorized owner to remint them through the ticket lifecycle) before claiming audited completion.

## 2026-08-15 — proposed plan amendment: T-31 (harness-sync migration is incomplete)

**Finding:** commit `c44f9af` deleted `.claude/hooks/claim_lookup.py` and
`.claude/hooks/verify_gate.sh`, plus `.claude/active-ticket`, but retained the
hook test suite and T-13/T-29 contracts that require them. It moved historical
receipts to `.claude/evidence-v1/*.pass`, while the new lifecycle resolves only
`.claude/evidence/<ticket>.json`. Consequently `uv run pytest -m "not live" -x
-q` fails at `backend/tests/hooks/test_claim_format.py::test_mode_last_ignores_session_entirely`
after 190 passing tests (missing `claim_lookup.py`), and the new harness treats
all historical dependencies as unresolved.

**Proposed ticket:** `T-31 — Complete the harness-sync migration and preserve auditable closure history`

- **Objective:** Reconcile the new session-claim/JSON-receipt harness with the
  tracked contracts, hook configuration, tests, and historical evidence left by
  `c44f9af`.
- **Acceptance:**
  1. The active lifecycle, configured hooks, and hook tests agree on one
     supported claim/close protocol; the hook test suite passes without merely
     deleting coverage for ownership, close gating, or fail-closed behavior.
  2. Historical `evidence-v1/*.pass` is reconciled under an explicit migration
     policy: either retained as clearly labeled non-auditable legacy closure
     records that the lifecycle can recognize, or restored/reminted as
     receipt-bound records. The policy must not fabricate commit hashes or
     fingerprints for historical bare timestamps.
  3. Every plan ticket marked `closed` has a lifecycle status consistent with
     the chosen migration policy; dependencies are not silently regressed to
     `queue` solely by the sync.
  4. A regression test proves both a legacy closure record and a new
     fingerprint-bound JSON receipt behave as specified, and an end-to-end
     non-live suite passes.
- **Verify:** `uv run pytest -m "not live" -q`
- **Scope:** `.claude/hooks/**`, `.claude/scripts/**`, `.claude/evidence-v1/**`,
  `.claude/evidence/**`, `.claude/settings.json`, `backend/tests/hooks/**`,
  `backend/tests/plan/**`, `docs/tickets.json`, `docs/TASKS.md`.
- **Depends on:** none.
- **Human decision required:** approve the historical-evidence migration policy
  above; the available `.pass` files contain only epochs and cannot honestly be
  upgraded to commit/fingerprint receipts by an agent.
- [2026-08-15T01:39:06Z] T-25 released by 1ed89054-d046-46fd-8a20-42f43c6ed16d: Close blocked by INTEGRITY FAIL on docs/tickets.json — a concurrent monitor session appended T-31 to the plan mid-ticket. Releasing and immediately re-claiming so the monitor's amendment lands in a fresh ticket-start commit rather than inside T-25's attested diff. T-25 work is complete and green in the tree.
