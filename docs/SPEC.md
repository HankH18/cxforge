# Othram AI Support Agent — Spec

Gauntlet challenger project (PRD_02, Othram). Greenfield. Planning finalized 2026-08-13.

## Problem & intent

Build an autonomous AI support agent that handles inbound Zendesk tickets for a
(fictionalized) forensic-genomics lab: answers routine inquiries grounded in a
knowledge base and a live case-management system, and escalates to humans only
when necessary. The graded primary metric is human-avoidance rate — tickets
resolved with zero human touch. A React review portal (draft feed + approval
gate) ships in this scope but exists to seed the post-submission product; the
graded behavior is autonomous mode.

## User-visible behavior

Actors: **customer** (writes into Zendesk), **reviewer** (uses the portal),
**operator** (runs demo/eval tooling).

### Ticket handling
- **R1** WHEN a ticket is created or a customer replies in Zendesk, THE SYSTEM
  SHALL receive the event via webhook and start an agent run, exactly once per
  (ticket, comment) pair, without re-triggering on its own updates.
- **R2** WHEN the inquiry is a case-status question and the case resolves in
  the case system, THE SYSTEM SHALL post a public reply whose case facts are
  template-filled from the lookup result, tag the ticket, and mark it solved.
- **R3** WHEN the inquiry is a permission request covered by the always-grant
  policy, THE SYSTEM SHALL grant it in a public reply and mark the ticket
  solved.
- **R4** WHEN the inquiry is a process/documentation/general question, THE
  SYSTEM SHALL answer from retrieved KB content only if the draft passes the
  groundedness verifier; otherwise escalate (R6).
- **R5** WHEN the inquiry is off-topic, THE SYSTEM SHALL reply with a polite
  redirect offering in-scope help, tag it `off-topic`, and leave the ticket
  open.
- **R6** WHEN any hard trigger fires (billing dispute, explicit human request,
  unknown/unresolvable case, out-of-procedure request, empty retrieval, low
  confidence, verifier failure) OR the classifier flags
  frustration/complexity above threshold, THE SYSTEM SHALL: post an internal
  note (conversation summary, grounded facts, escalation reason), tag, assign
  to the escalation group, and publicly tell the customer a specialist will
  follow up. Never guess.
- **R7** WHEN a customer replies on an open ticket, THE SYSTEM SHALL rebuild
  conversation context from the full Zendesk comment thread (stateless — no
  server-side conversation memory).
- **R8** In autonomous mode, p95 latency webhook-receipt → public reply SHALL
  be under 5 minutes, measured by the scenario runner.

### Grounding invariant
- **R9** No factual claim about a case SHALL appear in any outbound reply
  unless traceable to a field of a tool result in that run. Case facts are
  template-filled, never free-generated. Enforced by the grounding test suite
  including an adversarial set (unknown case IDs, leading questions asserting
  false facts).

### Portal
- **R10** THE SYSTEM SHALL expose a portal feed of every agent run: draft,
  sent body, route, confidence, escalation reason, and a trace link.
- **R11** A single boolean approval gate SHALL exist. ON: every outbound
  public reply is held as a draft for reviewer edit/approve/reject before
  send. OFF (default): autonomous send. No per-confidence granularity.
- **R12** Gated (approved/edited) sends SHALL be recorded as human-touched and
  excluded from the human-avoidance numerator.
- **R13** THE SYSTEM SHALL report: human-avoidance rate, p50/p95 latency, and
  escalation counts by reason.

### Portability & evaluation
- **R14** An `EmailAdapter` stub (fake transport) SHALL pass the identical
  `HelpdeskPort` contract test suite as `ZendeskAdapter`.
- **R15** An escalation eval report SHALL be produced from a human-approved
  labeled set (~50 tickets): confusion matrix, precision/recall/F1, PR curve
  with chosen threshold marked. Recall on the hard-trigger subset ≥ 0.95.

## Constraints

- Stack (locked in planning): Python 3.12 + FastAPI; LangGraph orchestration;
  OpenAI primary behind an `LLMClient` isolation layer (structured outputs,
  pinned model version); single Postgres 16 + pgvector for case data and KB;
  React + Vite + TypeScript portal; Promptfoo + DeepEval + Langfuse.
- Zendesk: 14-day trial account, OAuth 2.0 only (API tokens are being
  deprecated — never use them), dedicated AI agent user for attribution,
  trigger→webhook ingress with a nullifying loop-guard tag.
- Hosting: cloudflared tunnel in dev; single DigitalOcean droplet
  (docker-compose) for the demo. CI: minimal GitHub Actions (lint + unit only
  — Actions cost is a real constraint); promptfoo/e2e run locally.
- Data: fictional generic lab only. No Othram-real data, no scraping.
- Deliverables: source code, technical documentation, demo video.

## Non-goals

- No confidence-band gating — the gate is all-or-nothing (R11).
- No real email channel: EmailAdapter is a contract-proving stub.
- No Zendesk macros integration (preview-then-commit API deferred; tags,
  notes, status cover the API-usage criterion).
- No multi-tenancy, no real portal auth (single shared token), no user
  management.
- No CSAT measurement — designed-for, documented as not measurable without
  real customers.
- No model fine-tuning. No voice/chat channels. No KB ingestion pipeline
  (KB is seeded fixture content).
- Post-submission roadmap is out of this doc set entirely.

## Success criteria

1. The four canonical scenarios (status/no-escalation, permission/
   no-escalation, complex-technical/escalation, off-topic/boundary) plus one
   adversarial unknown-case ticket run end-to-end against the live Zendesk
   trial, on camera, with the Zendesk UI showing reply/note/tags/status.
2. Grounding suite green, including the adversarial set (R9).
3. Eval report artifact exists and meets R15 thresholds.
4. Contract suite green for both adapters (R14).
5. Portal demo: feed populated; gate flipped ON on camera showing the review
   queue, then an edited-approve send (R11–R12).
6. Scenario runner reports p95 < 5 min (R8); metrics endpoint serves R13.
7. Deployed instance reachable; technical documentation covers architecture,
   grounding design, escalation methodology + eval results, and the
   Zendesk OAuth/webhook setup runbook.

## Open questions

None — resolved in planning session 2026-08-13.
