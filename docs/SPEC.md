# Othram AI Support Agent — Spec

Gauntlet challenger project (PRD_02, Othram). Greenfield. Planning finalized 2026-08-13.

**Amended in place 2026-08-16** — see `docs/DECISIONS.md` (ADR-001…016) for the decision
record behind every amendment below; ADR-014 is the one that authorizes editing this file
at all. Amendments are additive: **R1–R15 are never renumbered, reworded away, or
deleted**, because graders and code reference them by number.

Read this alongside the three current entry points: `docs/STATE.md` (what is verified true
today, and what is not), `docs/BUILD-PLAN.md` (the remaining work, the waves, and the
frozen contracts) and `docs/DECISIONS.md` (why each choice was made). Where this doc and
those disagree about **status**, `docs/STATE.md` wins; this doc states intent, not
progress.

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
  - **R7.1** *(added 2026-08-16 — ADR-009.)* THE SYSTEM SHALL additionally fetch
    the requester's recent prior tickets from the helpdesk and surface that
    history to the classifier, so that a repeat complainer reads differently
    from a first-time asker in the R6 escalation judgment. The port method is
    `HelpdeskPort.fetch_requester_history(requester_email, *,
    exclude_ticket_id, limit=5) -> list[TicketSummary]`, pinned in
    `docs/DESIGN.md §Frozen interface contracts 1.5` and covered for **both**
    adapters by R14's suite. Still stateless: history is read from the helpdesk
    on every run and never persisted server-side. **Customer history is in
    scope** — it was the one PRD line item that appeared in neither the code
    nor the non-goals below, and that gap is what ADR-009 closes.
- **R8** In autonomous mode, p95 latency webhook-receipt → public reply SHALL
  be under 5 minutes, measured by the scenario runner.
  - *Definition note (2026-08-16 — ADR-004).* "Webhook receipt" means the
    moment ingress accepts the event, and that definition does not change. What
    changes is the code: today receipt time is minted inside the graph's
    **last** node (`backend/src/agent/nodes.py:591`), so the interval actually
    measured excludes every model call. No run has executed outside a test
    process either, which would let a metrics panel reading p95 = 0.0 satisfy
    success criterion 6 vacuously. See `docs/STATE.md §4.1` and
    `docs/DESIGN.md §Metric definitions`.

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

- Stack (locked in planning; provider amended 2026-08-16 — ADR-014, ADR-008):
  Python 3.12 + FastAPI; LangGraph orchestration; **Anthropic** primary behind
  an `LLMClient` isolation layer (structured outputs, pinned model version);
  single Postgres 16 + pgvector for case data and KB; React + Vite + TypeScript
  portal; Promptfoo + Langfuse. (**DeepEval removed 2026-08-16 — ADR-013/ADR-018 pass;
  see the note below.**)
  - **The provider pivot is recorded, not erased (ADR-014).** Planning locked
    OpenAI; the build ships Anthropic — `ANTHROPIC_MODEL = "claude-opus-5"`,
    pinned in one constant at `backend/src/agent/config.py:23` and consumed by
    `AnthropicLLMClient` in `backend/src/agent/llm.py`. The swap cost exactly
    one module and changed no caller, because every model call goes through the
    `LLMClient` seam. That is the isolation layer doing precisely the job it was
    specified to do, which is why the history stays in the doc.
  - **Embeddings are Voyage, not Anthropic (ADR-008).** Anthropic has no
    embeddings API, so the provider story is *Anthropic for generation, Voyage
    for embeddings* — `voyage-4-lite` with `output_dimension=1024`, which
    matches the existing `EMBEDDING_DIM = 1024` and the `kb_chunks.embedding
    vector(1024)` column, making it a reseed with no schema migration. Not built
    yet (gated on the owner's Voyage key — `docs/OWNER-ACTIONS.md`); the lexical
    `HashingEmbedder` in the tree today stays as the offline default so CI and
    the non-live suite need no network and no key.
  - **Promptfoo is now real; DeepEval is removed; Langfuse is still intent.**
    Updated 2026-08-16 after W1-E. **Promptfoo ships** — `promptfooconfig.yaml`
    drives the shipped `classify`/`compose` nodes through a custom provider, and
    the suite is proven to bind by degrading the real prompts: swapping two route
    definitions in `CLASSIFY_SYSTEM` takes classification from 18/20 to **10/20**,
    and weakening `KB_ANSWER_SYSTEM` takes grounding from 5/5 to **2/5**.
    **DeepEval is removed** from `pyproject.toml` and from the stack line above,
    settling ADR-013's condition on four measured grounds: zero imports repo-wide;
    it contradicts R9's design, which is enforced by a pure-Python,
    judge-independent `grounding_guard` *specifically* so a model-judged score
    cannot buy its way past it; `deepeval.metrics.utils.initialize_model` falls
    through to `OpenAIModel` when no model is passed, which would quietly
    reintroduce OpenAI to a codebase whose isolation story is the Anthropic pivot;
    and its metrics make live judge calls, breaking the offline guarantee of the
    gated suite. ADR-013's actual requirement — a second, independent evidence
    stream alongside `evals/report.py` — is met by promptfoo instead.
    `backend/tests/grounding/test_no_unused_eval_dependency.py` now enforces the
    rule rather than the outcome: it goes green either by removal *or* by making a
    declared eval dependency do real work. ADR-006 commits to real Langfuse
    instrumentation. Until those land, treat this bullet as a target.
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

**Customer history is deliberately absent from this list because it is IN scope**
(R7.1, ADR-009). Its earlier absence from *both* the code and this list was the
ambiguity ADR-009 resolves; it is not an omission and must not be read as one.

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
