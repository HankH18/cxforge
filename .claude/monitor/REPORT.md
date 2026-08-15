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

## 2026-08-15T20:41Z — cycle 2: T-27 closed, T-28 opened

### T-27 — verdict CONFIRMED (no finding)

The strongest close in the log so far. Checked, not taken on trust:

- Receipt binds to `c9c47be ticket-close: T-27`, ancestor of HEAD, subject
  matches. Fingerprint recomputes **exactly**. Receipt `verify` matches the plan.
- 14 files in the close range, **zero out of scope**.
- **Verify re-run independently by me** (not the build session):
  `uv run pytest backend/tests/hooks -q` → **297 passed**. Not a claim I inherited.
- **Green is evidence of the real thing.** `conftest.py` `_build_project` copies
  the *real* `.claude/scripts/` and `.claude/hooks/` into a disposable project and
  drives those (conftest.py:125-131) — not stubs. And the one thing a copy-based
  suite would miss, the live wiring, is covered separately:
  `test_settings_json_matcher_includes_notebookedit` reads the **real**
  `.claude/settings.json` and asserts NotebookEdit was *added to* rather than
  *replacing* `Edit|Write`. Live matcher confirmed `Edit|Write|NotebookEdit`.
- All three declared defects genuinely fixed: pathless deny-by-default with a
  documented empty allowlist; NotebookEdit both wired and normalised; stop guard
  fails closed on an unidentifiable session.

**Test-weakening check — passed, and this is the part worth reading.** One test
disappeared: `test_unidentifiable_session_never_blocked_via_any_claim`. It was
not deleted — it was renamed and **inverted** to
`test_unidentifiable_session_fails_closed_and_blocks`. Its previous form had
deliberately pinned the *bug*, with a docstring saying the behaviour was asserted
"so a future change to this behaviour is a conscious decision, not an accident."
T-27 is that conscious decision, and the new docstring says so explicitly. Two
counter-case tests were added so failing closed cannot deadlock a session
(identified-but-claimless still stops; `stop_hook_active` retains priority).
Net +33 tests, zero skips, zero xfails. This is how a test inversion should look.

### T-28 — pre-flight: three of five acceptance criteria have no target

Claimed at 20:38Z, same session, note acknowledging supersession. Verified
against the tree — the contract names artifacts that no longer exist:

| Acceptance | Names | Reality |
|---|---|---|
| 1 | `verify_gate` refuses to run a gate | no `verify_gate` hook exists; not in `settings.json` |
| 3 | `test_legacy_claim_line_allows_regardless_of_evidence` | no such test in the tree |
| 4 | retire the bare first line of `.claude/active-ticket` | file deleted in `c44f9af cc-factory: harness sync` — **and out of T-28's scope** (`.claude/hooks/**`, `backend/tests/hooks/**`) |

NEEDS_HUMAN already predicted this class ("T-22, T-28, T-29 satisfied by
supersession, unclosable as written"); this is the concrete confirmation. The
risk to watch at close is **not** dishonesty — it is that a ticket whose targets
are absent can be closed with absence-assertions that pass trivially, producing
green that proves nothing. T-27 handled exactly this shape well (it inverted a
test and said why). I will audit T-28's close specifically for: whether the new
tests would fail if the legacy authority *were* restored, and whether test edits
stayed inside acceptance 3's explicit pre-authorisation.

### Confirmed by observation

My own writes to `.claude/monitor/REPORT.md` and `.claude/NEEDS_HUMAN.md` landed
*inside* T-27's attested commit range and did not fail its integrity check —
empirical confirmation of the META_ALLOW/HARNESS_STATE exemption claimed at
cycle 0. Watchdog reporting is safe to run concurrently with a live close.

### Live state at cycle 0

T-27 claimed by session `1ed89054` since 20:09Z, 0 attempts, actively working
(heartbeat current). Uncommitted: `scope_guard.sh`, `stop_guard.sh`,
`settings.json`, `conftest.py` + two new `*_prep.py` — all inside T-27's declared
scope. Will audit at close.
