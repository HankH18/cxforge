# Othram AI Support Agent — Task Graph

Human-readable mirror. `docs/tickets.json` is authoritative — the hooks and
native-Tasks ingestion read it.

**Merge order for parallel worktrees**: ascending ticket ID, always
(e.g. T-1 merges before T-2; T-8 before T-9 before T-10).

**Pick order**: tickets carrying `"priority": "next"` are claimed BEFORE the
ascending-ID default. T-12–T-21 are that batch — a remediation set raised from
observed defects in the harness and the build. The three remaining original
tickets (T-7, T-10, T-11) are all blocked on human-only steps, so the priority
batch is what an agent can actually pick up. Merge order within the batch is
still ascending ID.

## Dependency graph

```mermaid
graph TD
    T0[T-0 Bootstrap] --> T1[T-1 Data layer: cases + KB]
    T0 --> T2[T-2 HelpdeskPort + ZendeskAdapter]
    T2 --> T3[T-3 EmailAdapter stub]
    T2 --> T4[T-4 Webhook ingress + runbook]
    T1 --> T5[T-5 Agent core graph]
    T2 --> T5
    T5 --> T6[T-6 Escalation engine]
    T6 --> T7[T-7 Labeled set + eval report]
    T5 --> T8[T-8 Portal API + gate]
    T6 --> T8
    T8 --> T9[T-9 Portal UI]
    T4 --> T10[T-10 Scenario runner + live e2e]
    T6 --> T10
    T7 --> T11[T-11 Deploy + demo + docs]
    T9 --> T11
    T10 --> T11
    T3 --> T11

    subgraph remediation["Priority batch — claimed first"]
        T12[T-12 Scope guard matching] --> T13[T-13 Session-scoped claims]
        T14[T-14 Verify blast radius]
        T15[T-15 Enforce human gate]
        T16[T-16 Test isolation]
        T17[T-17 Deploy verifier bug]
        T18[T-18 Classifier errors]
        T19[T-19 API contract binding]
        T20[T-20 Versioned migrations]
        T21[T-21 Eval measures real engine]
    end
    T7 --> T21
    T15 --> T21
```

Parallel waves are computed at execution time; disjoint-scope tickets are
marked `parallel_safe`.

## Tickets

### T-0: Repo bootstrap and test harness
- **Objective**: Monorepo skeleton so every downstream verify command runs.
- **Refs**: SPEC §Constraints, DESIGN §Verification strategy
- **Acceptance**: 1) `backend/` (FastAPI app stub), `portal/` (Vite React TS
  stub), `evals/`, `docs/` exist; 2) docker-compose brings up Postgres 16 +
  pgvector healthy; 3) pytest markers `contract`, `grounding`, `live`
  registered; 4) GitHub Actions workflow runs ruff + mypy + `pytest -m "not
  live"` only; 5) promptfoo config stub and `.env.example` present.
- **Verify**: `docker compose up -d db && uv run pytest && uv run ruff check . && uv run mypy backend`
- **Scope**: repo root (`pyproject.toml`, `uv.lock`, `.gitignore`,
  `docker-compose.yml`, `.env.example`, `promptfooconfig.yaml`), `backend/**`,
  `portal/**` (scaffold only), `evals/**`, `.github/**`
- **Depends on**: none
- **Non-goals**: no business logic, no portal components beyond scaffold.

### T-1: Data layer — case system and KB fixtures
- **Objective**: Fictional-lab case DB and KB the agent grounds in (R2, R4).
- **Refs**: R2, R4, DESIGN §Case system, §Data models
- **Acceptance**: 1) `cases` schema + idempotent seeder, ~30 cases covering
  every stage incl. edge cases (just-submitted, complete, stale); 2) ~15
  fictional SOP/policy/service docs authored under `fixtures/kb/`, chunked
  and embedded into `kb_chunks`; 3) typed lookup functions (by case_id, by
  requester_email) with miss returning a typed NotFound; 4) retrieval smoke
  test returns the relevant chunk for 5 sample queries.
- **Verify**: `uv run pytest backend/tests/data -q`
- **Scope**: `backend/src/data/**`, `fixtures/**`, `backend/tests/data/**`
- **Depends on**: T-0
- **Non-goals**: no ingestion pipeline; no real-lab content. `parallel_safe`
  with T-2.

### T-2: HelpdeskPort, ZendeskAdapter, contract suite
- **Objective**: The port boundary and its full Zendesk implementation (R1
  write-side, R14 foundation).
- **Refs**: R14, DESIGN §HelpdeskPort
- **Acceptance**: 1) Protocol + normalized models exactly as pinned; 2)
  OAuth client (no API tokens), backoff honoring `Retry-After`; 3) all port
  ops implemented — public reply and internal note as separate PUTs; every
  write appends the `ai-processed` tag; 4) contract suite written against
  the Protocol, parametrized by adapter, passing over mocked Zendesk HTTP;
  5) `scripts/live_smoke.py` exercising each op against a real trial (manual
  run, env-gated).
- **Verify**: `uv run pytest -m contract -q`
- **Scope**: `backend/src/helpdesk/**`, `backend/tests/contract/**`, `scripts/live_smoke.py`
- **Depends on**: T-0
- **Non-goals**: no macros; no email adapter (T-3). `parallel_safe` with T-1.

### T-3: EmailAdapter stub
- **Objective**: Prove the port is swappable — the differentiation artifact
  (R14).
- **Refs**: R14, DESIGN §HelpdeskPort
- **Acceptance**: 1) EmailAdapter over an in-memory fake transport passes the
  identical parametrized contract suite; 2) README section stating exactly
  what a production email channel would add (IMAP polling, threading via
  Message-ID) without implementing it.
- **Verify**: `uv run pytest -m contract -q`
- **Scope**: `backend/src/helpdesk/email_adapter.py`, contract test params
- **Depends on**: T-2
- **Non-goals**: no real SMTP/IMAP. Stub stays a stub. `parallel_safe` with
  T-4/T-5.

### T-4: Webhook ingress and Zendesk setup runbook
- **Objective**: Exactly-once, loop-safe ticket event intake (R1).
- **Refs**: R1, DESIGN §Webhook ingress
- **Acceptance**: 1) HMAC-verified endpoint matching the pinned payload; 2)
  idempotency via `tickets_seen` — duplicate (ticket, comment) events are
  no-ops; 3) events authored by the AI user are dropped; 4)
  `docs/zendesk-runbook.md` covers the human steps: trial signup, OAuth app,
  AI agent user, trigger with `tags not include ai-processed` nullifier,
  webhook + signing secret, cloudflared; 5) unit tests for HMAC
  reject/accept, dedupe, self-event drop.
- **Verify**: `uv run pytest backend/tests/ingress -q`
- **Scope**: `backend/src/ingress/**`, `backend/tests/ingress/**`, `docs/zendesk-runbook.md`
- **Depends on**: T-2
- **Non-goals**: no polling fallback unless the trial blocks webhooks (if it
  does: stop, re-plan). `parallel_safe` with T-5 after T-2.

### T-5: Agent core graph
- **Objective**: The LangGraph run: classify → route → ground → compose →
  verify → decide → act (R2–R5, R7, R9, R11-decide).
- **Refs**: R2–R5, R7, R9, DESIGN §Agent graph, §LLMClient
- **Acceptance**: 1) graph nodes/state exactly as pinned; 2) all model calls
  through `LLMClient`; OpenAI impl with strict structured outputs, pinned
  model constant; 3) case facts reach drafts only via templates fed by tool
  results; 4) verifier node scores KB drafts, threshold from config; 5) gate
  setting respected in `decide`; 6) graph tests with a fake LLMClient cover
  the four canonical scenarios end-to-end in-process; 7) grounding suite:
  adversarial unknown-case and false-premise inputs produce escalation or
  refusal, never invented facts.
- **Verify**: `uv run pytest backend/tests/graph -q && uv run pytest -m grounding -q`
- **Scope**: `backend/src/agent/**`, `backend/tests/graph/**`, `backend/tests/grounding/**`
- **Depends on**: T-1, T-2
- **Non-goals**: no checkpointing/interrupts/subgraphs; no escalation rule
  logic beyond calling T-6's interface (stub until T-6 lands).

### T-6: Escalation engine
- **Objective**: Hard rules + classifier + internal-note composition (R6).
- **Refs**: R6, DESIGN §Escalation contract
- **Acceptance**: 1) hard rules exactly as pinned, deterministic, unit-tested
  individually; 2) classifier via LLMClient emitting `EscalationCall`; 3)
  final-decision combinator (rule OR classifier≥threshold); 4) internal note
  contains summary, grounded facts, reason enum; customer notice posted; 5)
  wired into T-5's `decide`/`act`.
- **Verify**: `uv run pytest backend/tests/escalation -q`
- **Scope**: `backend/src/escalation/**`, `backend/tests/escalation/**`,
  `backend/src/agent/**`, `backend/tests/graph/**`, `backend/tests/grounding/**`
  (the two test dirs were added by amendment: wiring the classifier into the
  live graph requires registering `EscalationCall` in T-5's fakes)
- **Depends on**: T-5
- **Non-goals**: no threshold tuning (T-7 owns it); no sentiment model beyond
  the classifier prompt.

### T-7: Labeled set and escalation eval report
- **Objective**: The flagship credibility artifact — measured
  precision/recall on escalation (R15).
- **Refs**: R15, DESIGN §Verification strategy
- **Acceptance**: 1) `evals/labeled_set.yaml` with ~50 tickets spanning all
  routes, all hard triggers, fuzzy frustration/complexity cases, and
  adversarial phrasing; 2) **human gate: labels reviewed and approved by the
  project owner before use — record approval in the fixture header**; 3)
  promptfoo run + report generator producing confusion matrix, P/R/F1, PR
  curve image with chosen threshold marked; 4) recall ≥ 0.95 on the
  hard-trigger subset at the committed threshold; threshold written to
  config; 5) report lands in `docs/eval-report/`.
- **Verify**: `uv run python -m evals.report && uv run pytest backend/tests/evals -q`
- **Scope**: `evals/**`, `backend/tests/evals/**`, `backend/src/escalation/**`
  (the report is written into `docs/eval-report/`; the scope guard exempts
  `docs/` for every ticket)
- **Depends on**: T-6
- **Non-goals**: no expanding the set past ~60; no synthetic label approval —
  the human sign-off is external ground truth, not skippable.

### T-8: Portal API and approval gate
- **Objective**: Feed, draft edit/approve/reject, gate toggle, metrics (R10–R13).
- **Refs**: R10–R13, DESIGN §Portal API, §Metric definitions
- **Acceptance**: 1) endpoints exactly as pinned, `X-Portal-Token` auth; 2)
  gate ON holds drafts `pending`; approve sends via HelpdeskPort and records
  `gated_sent`; 3) metrics computed per the pinned definitions — gated sends
  excluded from the human-avoidance numerator; 4) API tests cover both gate
  states and metric math.
- **Verify**: `uv run pytest backend/tests/portal -q`
- **Scope**: `backend/src/portal/**`, `backend/tests/portal/**`
- **Depends on**: T-5, T-6
- **Non-goals**: no UI (T-9); no real auth/multi-user. `parallel_safe` with
  T-7.

### T-9: Portal UI
- **Objective**: The reviewer-facing React surface (R10–R12) and demo
  centerpiece.
- **Refs**: R10–R12, DESIGN §Portal API
- **Acceptance**: 1) feed view with route/confidence/reason/trace link; 2)
  draft detail with editable body, approve/reject; 3) gate toggle; 4) metrics
  panel (R13); 5) builds clean; component tests for gate and edit-approve
  flows against a mocked API.
- **Verify**: `cd portal && npm run build && npm test`
- **Scope**: `portal/**`
- **Depends on**: T-8
- **Non-goals**: no styling beyond clean-and-readable; no websockets —
  polling is fine. `parallel_safe` with T-7, T-10.

### T-10: Scenario runner and live e2e
- **Objective**: Prove the system against real Zendesk and measure R8.
- **Refs**: R8, SPEC §Success criteria 1 & 6
- **Acceptance**: 1) runner seeds the four canonical scenarios + adversarial
  unknown-case via Zendesk API (respecting rate limits); 2) asserts
  UI-visible effects by API read-back (reply present, note on escalation,
  tags, status); 3) emits latency report (p50/p95 webhook→reply); 4) p95 <
  5 min against the deployed or tunneled instance; 5) marked `-m live`,
  excluded from CI.
- **Verify**: `uv run pytest -m live -q` (env-gated; requires runbook completed)
- **Scope**: `backend/tests/live/**`, `scripts/scenario_runner.py`
- **Depends on**: T-4, T-6
- **Non-goals**: no load testing beyond demo volume.

### T-11: Deploy, demo assets, technical documentation
- **Objective**: Everything the grader touches (SPEC §Success criteria 5–7).
- **Refs**: SPEC §Constraints, §Success criteria
- **Acceptance**: 1) docker-compose deploy on a DigitalOcean droplet,
  reachable, env documented; 2) `docs/` assembled: architecture (+ diagram),
  grounding design, escalation methodology with the T-7 report, portability
  section (port + both adapters, naming Gorgias/Intercom/Front as future
  adapters), runbook; 3) demo script/shot list: five live scenarios, gate
  flip + edited-approve on camera, metrics panel, one Langfuse trace showing
  tool result → templated reply; 4) README quickstart verified from clean
  clone.
- **Verify**: `bash scripts/verify_deploy.sh` (health checks + docs links) 
- **Scope**: `docs/**`, `deploy/**`, `scripts/verify_deploy.sh`, README
- **Depends on**: T-3, T-7, T-9, T-10
- **Non-goals**: no video editing tooling; recording is a human step.

## Priority batch — remediation (T-12–T-21)

Raised from defects observed during the T-0–T-11 build and verified against
  the repository. These carry `"priority": "next"` in `docs/tickets.json`
  and are claimed before the ascending-ID default.

### T-12: Scope guard matches only intended paths
- **Objective**: scope_guard.sh admits paths it should deny and denies paths
  it should ignore; make its matching exact and give the hook its own tests.
- **Refs**: OBS#W2, OBS#W3, CLAUDE.md#rule-4
- **Acceptance**: 1) glob-to-regex match is anchored at BOTH ends; with
  scope portal/** the path backend/src/portal/routes.py is DENIED
  (regression test, this exact pair); 2) a path that does not resolve under
  CLAUDE_PROJECT_DIR exits 0 (out-of-repo scratch is never the scope guard's
  business), compared after realpath so ../ traversal cannot escape scope;
  3) a missing or empty .claude/active-ticket fails CLOSED (deny) rather
  than open, with an explicit documented sentinel for deliberate unclaimed
  work; 4) backend/tests/hooks/ drives the real hook with synthetic
  PreToolUse JSON over a table of (ticket, path, expect) pairs covering
  every ticket's scope in docs/tickets.json; 5) each new hook test is
  written FIRST and demonstrated FAILING against current HEAD (the portal/**
  vs backend/src/portal/routes.py pair, the out-of-repo path, and the
  missing-claim case) before the hook is changed — this ticket authors its
  own gate, so a recorded red-then-green transition is what makes that gate
  meaningful.
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**`, `backend/tests/hooks/**`
- **Depends on**: none
- **Non-goals**: No change to which paths any ticket's scope lists — this
  fixes the matcher, not the plan; No change to stop_guard/verify_gate
  session behaviour (T-13 owns that). Not `parallel_safe`.

### T-13: Session-scoped, append-only ticket claims
- **Objective**: Guards key off a single mutable untracked file with no
  notion of who claimed the ticket, so a second session in the same
  directory is told to finish or revert another session's work.
- **Refs**: OBS#W1, OBS#W7, OBS#W8
- **Acceptance**: 1) a claim records ticket id + CLAUDE_SESSION_ID + UTC
  timestamp; stop_guard and verify_gate act ONLY on a claim owned by the
  current session; 2) a second session in the same working directory is
  never blocked by another session's claim (fixture test asserting the
  observer case that fired on T-8, T-9 and T-11); 3) claim records are
  append-only and tracked in git, restoring the per-claim audit trail lost
  when .claude/active-ticket was untracked; 4) guards refuse to honour a
  claim whose ticket already has a passing .claude/evidence/<id>.pass; 5)
  worktree setup creates the checkout BEFORE the venv, and unlocks/prunes on
  exit; an unborn-HEAD worktree is treated as a setup failure to retry, not
  a reusable target; 6) the cross-session test is demonstrated FAILING
  against current HEAD (a second session blocked by another session's claim)
  before the guards are changed, for the same reason as T-12.
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**`, `backend/tests/hooks/**`
- **Depends on**: T-12
- **Non-goals**: No multi-agent scheduling or lock arbitration — this makes
  claims legible, it does not coordinate them. Not `parallel_safe`.

### T-14: Verify commands cover their blast radius
- **Objective**: A ticket's verify runs only that ticket's own suite while
  its scope permits changes that break others; T-11's verify is additionally
  a script inside T-11's own scope.
- **Refs**: OBS#W4, OBS#W5, OBS#W9, CLAUDE.md#rule-5
- **Acceptance**: 1) every ticket's verify runs its own suite plus every
  suite that imports from its scope paths (a reverse-dependency set), or the
  full suite where that is simpler; 2) no ticket's verify command references
  a path inside that same ticket's scope — a gate is never authored by the
  work it gates; 3) backend/tests/plan/ asserts both invariants above hold
  for EVERY ticket in docs/tickets.json, so a future ticket cannot
  reintroduce either; 4) docs/tickets.json gains a status field the hooks
  maintain, so ticket progress is readable across sessions instead of
  inferred from evidence files; 5) docs/TASKS.md re-synced to
  docs/tickets.json; 6) the two plan invariants are demonstrated FAILING
  against current docs/tickets.json before any verify command is rewritten —
  including on this batch's own tickets, which must end compliant.
- **Verify**: `uv run pytest backend/tests/plan -q`
- **Scope**: `docs/**`, `backend/tests/plan/**`, `.claude/hooks/**`
- **Depends on**: none
- **Non-goals**: No widening or narrowing of any ticket's scope globs; No
  retroactive re-verification of already-closed tickets. Not
  `parallel_safe`.

### T-15: Machine-enforce the human approval gate
- **Objective**: evals.report main() returns 0 unconditionally, so T-7's
  verify passes green with labels no human has approved; the project's one
  inviolable rule is its only unenforced one.
- **Refs**: OBS#W6, OBS#G7, SPEC#T-7-human-gate, CLAUDE.md#human-only-steps
- **Acceptance**: 1) python -m evals.report exits NON-ZERO while
  approval.status != APPROVED or approved_by/approved_date are empty; 2) an
  explicit opt-in flag permits a draft run for development; T-7's verify
  command does not pass that flag; 3) tests cover both directions: real
  unapproved fixture exits non-zero; a synthetic fully-approved fixture in
  tmp_path exits zero; 4) the three existing not-yet-approved tripwire
  assertions are rewritten to assert the APPROVED invariants (report renders
  FINAL, metrics.approved is True, signoff fields non-empty) so a genuine
  human approval turns the suite green rather than red; 5) docs/tickets.json
  T-7 verify updated to assert approval.
- **Verify**: `uv run pytest backend/tests/evals -q`
- **Scope**: `evals/**`, `backend/tests/evals/**`, `docs/**`
- **Depends on**: none
- **Non-goals**: NEVER approves labels or fills signoff fields — that is the
  human step this ticket exists to protect; No threshold tuning and no
  change to the labeled set's contents. `parallel_safe`.

### T-16: Test isolation and suite hygiene
- **Objective**: One shared database with per-test TRUNCATE means concurrent
  runs corrupt each other; the suite also rewrites a tracked file, and three
  conftest skip guards silently do nothing.
- **Refs**: OBS#G4, OBS#G8, OBS#W10, OBS#W12, CLAUDE.md#rule-9
- **Acceptance**: 1) each pytest process gets its own database or Postgres
  schema, derived from the xdist worker / worktree identity, so two
  concurrent full runs both pass (demonstrate by running two
  simultaneously); 2) no test writes into docs/ — the unapproved-report test
  reads committed artefacts read-only and generates into tmp_path; git
  status is clean after a full run, asserted by a test; 3) the pytestmark
  skip guards in the graph, grounding and portal conftest.py files are moved
  somewhere they actually take effect (a pytest_collection_modifyitems hook,
  or per-module pytestmark) — pytest ignores conftest-level pytestmark for
  sibling test modules, so today they never fire; 4) CI keeps its no-skip
  guard and gains a collected-count floor, so a suite that silently stops
  running is caught by number.
- **Verify**: `uv run pytest -q`
- **Scope**: `backend/tests/**`, `backend/src/data/**`, `evals/report.py`,
  `docker-compose.yml`, `.github/**`
- **Depends on**: none
- **Non-goals**: No new test cases for product behaviour — this is isolation
  and hygiene only; No weakening of any existing assertion to make a suite
  parallel-safe. Not `parallel_safe`.

### T-17: Deploy verifier honours an exported DEPLOY_HOST
- **Objective**: verify_deploy.sh sources .env with set -a before reading
  DEPLOY_HOST, and .env defines it as an empty assignment, so an exported
  value is silently clobbered and the run falls to LOCAL mode while printing
  PASS.
- **Refs**: OBS#G2, T-11#acceptance-1
- **Acceptance**: 1) DEPLOY_HOST=<host> bash scripts/verify_deploy.sh takes
  the REMOTE branch; an exported value is never overwritten by sourcing .env
  (regression test with a fake env file reproducing the clobber); 2) local
  mode requires an explicit opt-in flag and can never be mistaken for
  droplet evidence; without it, an empty DEPLOY_HOST is a hard failure
  rather than a silent local PASS; 3) T-11's acceptance criterion 1 can only
  be satisfied by a remote-mode run against a real droplet; 4)
  docs/deploy.md and .env.example updated to match the corrected precedence;
  5) the clobber is reproduced FAILING against current HEAD (DEPLOY_HOST
  exported, run takes LOCAL and exits 0) before scripts/verify_deploy.sh is
  touched.
- **Verify**: `uv run pytest backend/tests/deploy -q`
- **Scope**: `scripts/verify_deploy.sh`, `backend/tests/deploy/**`,
  `docs/**`, `.env.example`
- **Depends on**: none
- **Non-goals**: Does not create a droplet or perform a deploy —
  provisioning stays a human-authorised step; No change to the deploy stack
  itself (deploy/**). `parallel_safe`.

### T-18: Classifier errors stop masquerading as escalations
- **Objective**: run_classifier catches bare Exception and returns None,
  which is the pinned abstention condition and therefore a hard escalation
  trigger — so any bug in that path becomes a plausible-looking escalation,
  silently and unlogged.
- **Refs**: OBS#G3, OBS#W11, DESIGN#escalation-contract
- **Acceptance**: 1) the except is narrowed to the
  API/timeout/parse/validation errors it actually intends to absorb; every
  swallow is logged with the exception type; 2) a programming error raised
  inside the classifier path propagates instead of being converted to
  abstention (test asserts the exception escapes); 3) genuine abstention
  semantics are unchanged — a refusal or unparseable verdict still
  escalates, and every existing escalation test stays green; 4) the three
  duplicated FakeLLMClient copies assert the classifier was consulted rather
  than only defaulting its response away, so an unanticipated call site is
  loud again.
- **Verify**: `uv run pytest backend/tests/escalation backend/tests/graph backend/tests/grounding -q`
- **Scope**: `backend/src/escalation/**`, `backend/tests/escalation/**`,
  `backend/tests/graph/**`, `backend/tests/grounding/**`
- **Depends on**: none
- **Non-goals**: No change to the hard-rule set, the combinator, or the
  confidence threshold; No change to which conditions escalate — only to
  which FAILURES are allowed to look like them. `parallel_safe`.

### T-19: Bind the portal API contract
- **Objective**: portal/src/api.ts and backend/src/portal/schemas.py agree
  today purely by hand; nothing — no test, codegen step or CI job — fails if
  they drift, so a renamed field breaks only the live UI.
- **Refs**: OBS#G6, DESIGN#portal-api
- **Acceptance**: 1) the TypeScript request/response types are generated
  from the FastAPI OpenAPI schema, or a check asserts field-name, order and
  nullability parity between the two; 2) regenerating against the current
  backend produces types byte-identical to what is committed; 3) a
  deliberate backend field rename FAILS the check (demonstrate in the test,
  not by hand); 4) the check runs in CI on every push, not only when a human
  remembers; 5) the parity check is demonstrated FAILING against a
  deliberately renamed backend field before being wired into CI.
- **Verify**: `uv run pytest backend/tests/portal -q && cd portal && npm run build && npm test`
- **Scope**: `portal/**`, `backend/src/portal/**`,
  `backend/tests/portal/**`, `.github/**`
- **Depends on**: none
- **Non-goals**: No redesign of the API shape or the portal UI; No new
  endpoints. `parallel_safe`.

### T-20: Versioned schema migrations
- **Objective**: A single ad-hoc _MIGRATIONS string now re-executes in full
  on every production container start, with no record of what has been
  applied; it survives only because its one statement happens to be
  idempotent.
- **Refs**: OBS#G5, DESIGN#data-models
- **Acceptance**: 1) numbered migration files plus a schema_migrations
  ledger table recording what has been applied, so each migration runs
  exactly once; 2) the existing runs.reasons column is expressed as the
  first migration; a database created before it still upgrades in place, and
  a fresh database ends in the identical schema (both proven by test); 3)
  container bootstrap applies only unapplied migrations rather than
  re-running the full list on every boot; 4) a deliberately non-idempotent
  migration is applied exactly once across repeated init_schema calls
  (test); 5) a test proves a pre-existing database created WITHOUT
  runs.reasons is upgraded in place, run against a database built from the
  pre-T-8 schema.
- **Verify**: `uv run pytest backend/tests/data -q`
- **Scope**: `backend/src/data/**`, `backend/tests/data/**`,
  `deploy/backend/**`
- **Depends on**: none
- **Non-goals**: No schema changes beyond formalising the existing
  runs.reasons migration; No ORM adoption — this is migration bookkeeping
  only. Not `parallel_safe`.

### T-21: Escalation eval measures the real engine
- **Objective**: evals/report.py never imports EscalationEngine or
  run_classifier — it reimplements the precedence and fills the rest from
  three hand-authored replay tables, so its 1.0 scores grade a parallel
  implementation rather than the shipped engine.
- **Refs**: OBS#G1, OBS#A2, SPEC#R6, DESIGN#escalation-contract
- **Acceptance**: 1) the report calls
  escalation.engine.EscalationEngine.evaluate directly;
  STUB_CLASSIFIER_VERDICTS, STUB_STRUCTURAL_REASON and STUB_ABSTENTION_IDS
  are deleted, not merely bypassed; 2) the classifier half runs against a
  live LLMClient when OPENAI_API_KEY is present; without it the report FAILS
  rather than silently substituting fabricated verdicts; 3) a test asserts
  the report and the engine cannot diverge — the report has no escalation
  decision logic of its own; 4) the recommended threshold is swept over real
  classifier confidences, and the report states the sample size and date of
  the run that produced it; 5) recall >= 0.95 on the hard-trigger subset is
  measured against the real engine, per T-7 acceptance 4.
- **Verify**: `uv run pytest backend/tests/evals -q`
- **Scope**: `evals/**`, `backend/tests/evals/**`, `docs/eval-report/**`
- **Depends on**: T-7, T-15
- **Non-goals**: Does not approve the labeled set — T-7's human gate still
  governs whether any number here is publishable; No relaxing of the >= 0.95
  hard-trigger recall bar to make a real measurement pass. Not
  `parallel_safe`.
