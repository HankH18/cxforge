# cxforge

Othram AI Support Agent — a Gauntlet challenger project.

**The build harness is retired.** The ticket claim/close/receipt lifecycle ended at 30 of
32 tickets on 2026-08-16 (`docs/DECISIONS.md` ADR-001). Remaining work is ordinary
engineering, gated by the full test suite.

Current working agreement:
@.claude/rules/build-protocol.md

## Active docs — read in this order

| Doc | What it is |
|---|---|
| `docs/STATE.md` | Verified current state. Start here. |
| `docs/BUILD-PLAN.md` | Remaining work: waves, frozen contracts, file-ownership matrix. |
| `docs/DECISIONS.md` | Owner decisions and their rationale (ADR-001…016). |
| `docs/OWNER-ACTIONS.md` | What is blocked on the human, with exact commands. |
| `docs/SPEC.md` · `docs/DESIGN.md` | Requirements (R1–R15) and pinned contracts. |

## Historical — do not act on without checking the code first

`docs/HANDOFF.md`, `.claude/NEEDS_HUMAN.md`, `.claude/rules/harness-protocol.md`,
`docs/tickets.json`, `docs/TASKS.md`, `.claude/evidence/`.

These are accurate about problems that were solved and stale about problems that are
live. `.claude/evidence/` is a frozen attestation record — never write to it.
