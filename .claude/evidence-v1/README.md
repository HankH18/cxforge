# evidence-v1/ — inert legacy closure records

This directory holds the 18 receipt files the v1 build harness wrote before
commit `c44f9af` ("cc-factory: harness sync") replaced it. They were moved
here, unmodified, at migration time.

## What a file here is

Each `<T-id>.pass` file is exactly one thing: a bare Unix epoch timestamp
(e.g. `1786616551`), and nothing else. No commit hash. No content
fingerprint. No session identifier. No verify command. That is the entire
v1 receipt format — it recorded only *when* a verify_gate.sh run last
passed, not *what* was verified or *against which tree*.

## What a file here is NOT

It is **not** evidence the current (v2) lifecycle recognizes, honours, or
will ever recognize. `.claude/scripts/harness_lib.py`'s `receipt()` reads
`.claude/evidence/<T-id>.json` only. Nothing in the harness — `status()`,
`cmd_claim`, `cmd_close`, the scope guard, the task gate — reads, globs, or
even knows this directory exists. A ticket with a file here and no
`.claude/evidence/<id>.json` derives as `queue` today, exactly like a
ticket with no history at all, and remains claimable.

## Why these are retained rather than deleted or upgraded

They are retained as **history** — proof that these 18 tickets passed
their verify command at some point under the v1 harness — nothing more.

They are explicitly **not upgraded** into `.claude/evidence/<id>.json`
receipts, and never will be by any automated process. A v2 receipt's
`commit` and `fingerprint` fields attest to a specific tree: the commit
HEAD was at when the receipt was minted, and a sha256 over the ticket's
scope files' actual content at that moment. A bare epoch carries neither.
Synthesizing a plausible-looking commit hash or fingerprint for one of
these tickets now — from git history, from the current tree, from
guesswork — would fabricate an attestation the v1 harness never made and
the current tree cannot honestly reconstruct. T-31's own non-goals forbid
exactly this: *"No fabricated historical commit or fingerprint metadata."*

The only way a ticket listed below becomes auditable under the v2 contract
is the same way any other ticket does: **re-run the real lifecycle** —
`claim.sh claim <id> "<note>"` followed by a real `close` — which mints a
genuine `.claude/evidence/<id>.json` bound to the actual tree at close
time. Nothing here shortcuts that.

## Tickets with a v1 record here

`T-0, T-1, T-2, T-3, T-4, T-5, T-6, T-8, T-9, T-12, T-13, T-14, T-15, T-16,
T-17, T-18, T-19, T-20`

(18 files. `docs/tickets.json` marks these — and only these — `"status":
"closed"`; that field is itself inert under v2 and carried here only as a
cross-check, see `backend/tests/plan/test_status_field.py`.)
