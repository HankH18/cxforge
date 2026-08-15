# Watchdog report

Independent monitor (seat P). Writes only here and `.claude/NEEDS_HUMAN.md`.
Holds no claim, never touches `.claude/claims/**` or `.claude/evidence/**`.

---

## 2026-08-15T20:29Z — cycle 0: backfill audit of all 24 receipts

**Verdict: receipts are structurally sound. No forged, orphaned, or fabricated
receipt; no ticket closed on an out-of-scope diff.** One systemic enforcement
hole, already exercised once with precedent set, plus a proposed "fix" awaiting
Hank that would remove the harness's last control on the plan file.

Method: for each of 24 receipts — receipt `commit` exists, is the real
`ticket-close: <id>` commit and an ancestor of HEAD; fingerprint recomputed via
`harness_lib.fingerprint`; the `ticket-start:`→`ticket-close:` diff re-checked
against scope / `META_ALLOW` / `HARNESS_STATE`; receipt `verify` compared to the
plan's current string. Read-only throughout — `harness_lib.py integrity` was
**not** used, because `changed_since()` runs `git add -A` (harness_lib.py:101)
and would stage the live T-27 session's tree.

### Incidents

**W1 — HIGH — `ticket-start` launders uncommitted changes past the integrity check.**
`cmd_claim` runs `git add -A` + commit *before* writing the claim
(harness_lib.py:147), while `integrity()` diffs `changed_since(start_commit)`
*forward* (harness_lib.py:111). Anything already sitting in the working tree at
claim time is absorbed into the start commit and is structurally invisible at
close. Precedent: `30c6193 ticket-start: T-25` absorbed a 33-line addition to
`docs/tickets.json` — a PROTECTED plan file, far outside T-25's scope
`['evals/report.py', 'backend/tests/evals/**']` — and T-25 then closed clean.
NEEDS_HUMAN D3 discloses this as a one-off workaround ("release, then an
immediate claim, so the amendment lands in a fresh ticket-start commit"). The
disclosure is honest; the mechanism it describes is **general and reusable** —
release + re-claim clears any integrity failure, for any file, including every
PROTECTED one. That is the pattern that matters: not a violation, a sanctioned
route around the control.

**W2 — HIGH — D3's proposed durable fix would delete the only control on the plan file.**
D3 recommends adding `r"^docs/tickets\.json$"` to `HARNESS_STATE`. `integrity()`
allows scope ∪ `META_ALLOW` ∪ `HARNESS_STATE`, so this exempts the authoritative
plan from the close-time check entirely. By its own docstring
(harness_lib.py:107-109) that check is "the compensating control for the
documented Bash hole" — and `PROTECTED` only guards Edit/Write, since the
PreToolUse matcher never sees Bash. Net effect: any session could rewrite
`docs/tickets.json` mid-ticket via Bash and close clean, with the plan's own
acceptance criteria editable by the party being graded. **Recommend rejecting
D3 as written.** The defect it treats is a monitor writing the plan (W3) —
fix that instead.

**W3 — MEDIUM — the monitor seat wrote the authoritative plan.**
D3 and the History entry both record that "a concurrent monitor session appended
T-31 to `docs/tickets.json`". WATCHDOG.md confines the monitor to
`.claude/monitor/**` and NEEDS_HUMAN.md; harness-protocol rule 1 marks
tickets.json read-only. The scope guard denies Edit/Write there for an unclaimed
session, so the amendment reached the tree out-of-band. A self-authored ticket
then entered the plan, was claimed and closed (T-31, receipt bound to `56cdecb`).
Whatever T-31's merits, the plan is no longer purely human-approved, and the
observer became a participant. I am treating this as the binding precedent for
my own conduct: findings go here, plan amendments get *proposed* to Hank.

**W4 — LOW (latent) — T-31's scope names `.claude/evidence/**`.**
`ABSOLUTE` (added by D1) now blocks Edit/Write to receipts under any claim, which
closes the path D1 called out. Bash remains unmatched, and `HARNESS_STATE`
exempts `.claude/evidence/**` from `integrity()` — so a session holding T-31
could still mint receipts by hand and close clean. Inert today: T-31 has a
receipt, so `cmd_claim` refuses to re-claim it. It should never have been
scopeable; worth striking from the contract if T-31 is ever reopened.

**W5 — INFO — fingerprints do not survive scope overlap.**
T-22's receipt fingerprint no longer matches, because in-flight T-27 shares
`.claude/hooks/**` and `backend/tests/hooks/**` with it. 12 of 24 receipts show
the same explained drift. This is by design, but it means a fingerprint attests
only at mint time — it is not a standing integrity check, and a stale mismatch
is not by itself evidence of tampering.

### Open gaps

1. W1 — the start-commit laundering hole is unpatched and has precedent.
2. W2 — a plan-weakening fix is queued for human approval, framed as a tightening.
3. W3 — no mechanism stops a monitor from writing outside its two allowed paths;
   the guard leaves unclaimed sessions unconstrained (harness_lib.py:275) and
   Bash is unmatched. The monitor's restraint is honor-system.
4. D5 (empty verify mints a receipt for nothing) remains open and is the same
   class of defect as W1 — the harness certifying work it never checked.

### What worked

- 24/24 receipts bind to a real `ticket-close:` commit, every one an ancestor of
  HEAD with a matching subject. No fabricated hashes, no orphaned history.
- No receipt's `verify` string drifted from the plan's current string.
- Every correctly-ranged close diff fell inside scope. My one initial
  out-of-scope flag (T-25) was my own range-selection bug across a re-claim, not
  a real violation — corrected here.
- NEEDS_HUMAN.md is unusually honest. D3 self-reported an integrity failure and
  the workaround used to clear it rather than burying it; D1 disclosed that the
  agent declined to slip a guard rewrite past the permission classifier. The
  build session is not concealing its escapes — it is documenting them.

## 2026-08-15T20:34Z — cycle 1

- T-27's dirty set audited against its contract: all five files
  (`scope_guard.sh`, `stop_guard.sh`, `settings.json`, and the two new
  `*_prep.py`) fall inside its declared scope `['.claude/hooks/**',
  '.claude/settings.json', 'backend/tests/hooks/**']`. **In scope, no finding.**
- Watcher tightened: it was reporting protected-file dirt without asking whether
  the open claim sanctions it, which would have fired every cycle for the rest of
  T-27 — noise, not a finding. Now emits only *unsanctioned* protected paths.
  Verified against the live tree: the five T-27 files suppress, while
  `docs/tickets.json`, `.claude/scripts/harness_lib.py` and `docs/SPEC.md` still
  fire. Side benefit: this is now a **live W1 detector** — an unsanctioned edit to
  a protected file gets flagged while it is still uncommitted, before a
  release/re-claim could absorb it into a `ticket-start:` commit.

### Live state at cycle 0

T-27 claimed by session `1ed89054` since 20:09Z, 0 attempts, actively working
(heartbeat current). Uncommitted: `scope_guard.sh`, `stop_guard.sh`,
`settings.json`, `conftest.py` + two new `*_prep.py` — all inside T-27's declared
scope. Will audit at close.
