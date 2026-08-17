# Abandoned harness worktrees — archive

Captured **2026-08-16**, immediately before the 17 worktrees under `.claude/worktrees/`
were removed with `git worktree remove --force`.

## What this is

The cc-factory ticket harness ran its tickets in per-ticket git worktrees. When the
harness was retired, 17 of those worktrees were left on disk, every one of them with
uncommitted changes. They occupied **6.5 GB** — almost entirely per-worktree copies of
`.venv` and `node_modules`; the actual tracked content was ~2 MB per tree.

This directory is the recoverable record of everything those worktrees contained that
was not already in git. The worktrees themselves are gone; nothing here is.

## Why it exists

`docs/DECISIONS.md` **ADR-001** retired the cc-factory ticket harness entirely (no more
claim / close / receipt lifecycle). Nearly all the work stranded in these worktrees was
harness machinery — `harness_lib.py`, `gen_tasks.py`, `WATCHDOG.md`, `INGEST.md`, and
hook tests for harness invariants — i.e. work on a system that no longer ships.

The owner approved reclaiming the 6.5 GB on 2026-08-16, on the condition that the work
be **recoverable rather than merely assumed dead**. Hence this archive.

## Layout

```
abandoned-worktrees/
├── README.md              ← this file
├── INVENTORY.md           ← one table row per worktree: branch, base SHA, counts, size
└── <worktree-name>/
    ├── meta.txt           ← branch, head SHA, ancestor-of-master, dirty/untracked counts
    ├── status.porcelain   ← verbatim `git status --porcelain`
    ├── tracked.diff       ← verbatim `git diff HEAD` (staged + unstaged, vs the base SHA)
    └── untracked/         ← byte-for-byte copies of untracked files, at their repo paths
```

`untracked/` only exists for the 6 worktrees that had untracked files. `git status`
respects `.gitignore`, so `.venv/` and `node_modules/` were never candidates; every
untracked file captured is small text (`.py` / `.json`, 8 files, ~160 KB total). No
binaries were encountered, so no size-substitution was needed.

## How to recover a worktree's work

Each `tracked.diff` is a normal patch against the **base SHA recorded in that
worktree's `meta.txt`** (also in `INVENTORY.md`). To restore one:

```bash
cd /path/to/cxforge
git switch --detach <base SHA from meta.txt>
git apply .claude/harness-archive/abandoned-worktrees/<name>/tracked.diff
cp -R .claude/harness-archive/abandoned-worktrees/<name>/untracked/. .
```

Every one of these patches was verified to apply cleanly to a clean extraction of its
own base tree before the worktrees were deleted — see "Verification" below.

## The two things worth knowing about

### 1. `archive/wf_9910d138-f96-12` — the only commit not in master

Sixteen of the seventeen branch heads were already ancestors of `master`, so deleting
their branches lost nothing. **One was not.** Worktree `wf_9910d138-f96-12` sat on
commit `dc972bf` — *"baseline: pending unattributed harness_lib patch (not mine)"* — a
+73/−2 change to `.claude/scripts/harness_lib.py` that was never merged.

Before its branch was deleted, that commit was preserved as an annotated tag:

```bash
git show archive/wf_9910d138-f96-12        # the commit itself
git log -1 archive/wf_9910d138-f96-12      # subject + provenance
```

The tag is the *only* thing keeping `dc972bf` reachable. Do not delete it without
deciding deliberately that the patch is dead. Note that the commit subject itself flags
the patch as unattributed — its author was never established, which is part of why it
was left as a "baseline" rather than merged.

### 2. `test_supersede_terminal_state.py` — the unfinished W8 attempt

The single piece here with any plausible future value. `wf_9910d138-f96-12` also carried
an untracked, unfinished test:

```
wf_9910d138-f96-12/untracked/backend/tests/hooks/test_supersede_terminal_state.py   (27 KB)
```

It is an attempt at harness defect **W8** — *"no verified-unachievable/superseded
terminal state exists"* — recorded in `docs/STATE.md §7`. The harness only had
`resolved` and `queued`; there was no way to mark a ticket as verified-unachievable, so
work that could never be completed could never be honestly closed.

W8 is a *harness* defect and the harness is retired (ADR-001), so the test as written
targets a system that no longer ships. It is kept because the underlying idea — that a
work-tracking system needs a terminal state for "this cannot be done", distinct from
"not done yet" — outlives the harness and may be worth re-expressing if any future
tracking mechanism is built. **Do not restore it as-is**; it imports harness modules
that are retired.

Three sibling worktrees carried similar unfinished harness-invariant tests, archived on
the same basis: `test_claim_attribution_coherence.py` (f96-11),
`test_changed_since_no_staging.py` (f96-14), `test_receipt_revalidation.py` (b20-8), plus
citation-integrity work in `f96-13` and `b20-7`.

## A note on `agent-a767ac3940f609ac7`

This one looks alarming — 135 dirty files — and is the least valuable of the seventeen.
It sat on an **unborn branch** (`refs/heads/worktree-agent-a767ac3940f609ac7` never
existed; `git worktree list` showed HEAD as `0000000`), with 134 files staged and 1
modified.

Its staged index tree, `2224a6bb696f1173e91e7ee91be3764d7a7761ab`, is **byte-identical
to the tree of commit `200ccc6`** ("T-9: start — claim Portal UI"), which is already an
ancestor of `master`. The 135 files therefore contain **zero unique content** — they are
a stale snapshot of an old commit, not unmerged work.

The only content unique to that worktree is one line, captured in its `tracked.diff`:
`.claude/active-ticket` going from `T-9` to `T-7`. To reconstruct the full 135-file
state: `git archive 2224a6b | tar -x`.

## Verification performed before deletion

1. **Coverage** — all 17 worktrees have an archive entry; `status.porcelain` line counts
   match live `git status --porcelain` counts exactly, per worktree.
2. **Untracked fidelity** — all 8 untracked files verified byte-identical to source by
   MD5.
3. **Diff recoverability** — each `tracked.diff` was applied with `git apply --check`
   against a *clean* extraction (`git archive <base> | tar -x`) of its own base tree.
   All 16 non-empty diffs passed. The one 0-byte diff (`wf_b97a3bf5-b20-7`) is correct:
   that tree's 3 dirty entries were *all* untracked files, so there was no tracked change
   to capture.
4. **Ancestry** — every deleted `worktree-*` branch was confirmed either an ancestor of
   `master` or preserved by the `archive/wf_9910d138-f96-12` tag.

Archive total size: ~712 KB.
