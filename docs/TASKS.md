# Task Graph (GENERATED from tickets.json — do not hand-edit)

### T-0: Repo bootstrap and test harness  `[resolved]`
- **Objective**: Monorepo skeleton so every downstream verify command runs.
- **Acceptance**: 1) backend/ (FastAPI app stub), portal/ (Vite React TS stub), evals/, docs/ exist 2) docker-compose brings up Postgres 16 + pgvector healthy 3) pytest markers contract, grounding, live registered 4) GitHub Actions workflow runs ruff + mypy + pytest -m 'not live' only 5) promptfoo config stub and .env.example present
- **Verify**: `docker compose up -d db && uv run pytest -m "not live" && uv run ruff check . && uv run mypy backend && (cd portal && npm run build && npm test)`
- **Scope**: `backend/**, portal/**, .github/**, docker-compose.yml, pyproject.toml, .env.example, promptfooconfig.yaml, evals/**, .gitignore, uv.lock`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No business logic; No portal components beyond scaffold

### T-1: Data layer: case system and KB fixtures  `[resolved]`
- **Objective**: Fictional-lab case DB and KB the agent grounds in (R2, R4).
- **Acceptance**: 1) cases schema + idempotent seeder, ~30 cases covering every stage incl. edge cases (just-submitted, complete, stale) 2) ~15 fictional SOP/policy/service docs authored under fixtures/kb/, chunked and embedded into kb_chunks 3) typed lookup functions (by case_id, by requester_email) with miss returning a typed NotFound 4) retrieval smoke test returns the relevant chunk for 5 sample queries
- **Verify**: `uv run pytest -m "not live" backend/tests/data backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/data/**, fixtures/**, backend/tests/data/**`
- **Depends on**: T-0 · **parallel_safe**: true
- **Non-goals**: No ingestion pipeline; No real-lab content

### T-2: HelpdeskPort, ZendeskAdapter, contract suite  `[resolved]`
- **Objective**: The port boundary and its full Zendesk implementation (R1 write-side, R14 foundation).
- **Acceptance**: 1) Protocol + normalized models exactly as pinned in DESIGN 2) OAuth client (no API tokens), backoff honoring Retry-After 3) all port ops implemented; public reply and internal note as separate PUTs; every write appends the ai-processed tag 4) contract suite written against the Protocol, parametrized by adapter, passing over mocked Zendesk HTTP 5) scripts/live_smoke.py exercising each op against a real trial (manual run, env-gated)
- **Verify**: `uv run pytest -m "not live" backend/tests/contract backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/helpdesk/**, backend/tests/contract/**, scripts/live_smoke.py`
- **Depends on**: T-0 · **parallel_safe**: true
- **Non-goals**: No macros; No email adapter (T-3)

### T-3: EmailAdapter stub passes the contract suite  `[resolved]`
- **Objective**: Prove the port is swappable — the differentiation artifact (R14).
- **Acceptance**: 1) EmailAdapter over an in-memory fake transport passes the identical parametrized contract suite 2) README section stating exactly what a production email channel would add (IMAP polling, threading via Message-ID) without implementing it
- **Verify**: `uv run pytest -m "not live" -q`
- **Scope**: `backend/src/helpdesk/email_adapter.py, backend/tests/contract/**, README.md`
- **Depends on**: T-2 · **parallel_safe**: true
- **Non-goals**: No real SMTP/IMAP — the stub stays a stub

### T-4: Webhook ingress and Zendesk setup runbook  `[resolved]`
- **Objective**: Exactly-once, loop-safe ticket event intake (R1).
- **Acceptance**: 1) HMAC-verified endpoint matching the pinned payload 2) idempotency via tickets_seen — duplicate (ticket, comment) events are no-ops 3) events authored by the AI user are dropped 4) docs/zendesk-runbook.md covers the human steps: trial signup, OAuth app, AI agent user, trigger with 'tags not include ai-processed' nullifier, webhook + signing secret, cloudflared 5) unit tests for HMAC reject/accept, dedupe, self-event drop
- **Verify**: `uv run pytest -m "not live" backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/ingress/**, backend/tests/ingress/**, docs/zendesk-runbook.md`
- **Depends on**: T-2 · **parallel_safe**: true
- **Non-goals**: No polling fallback unless the trial blocks webhooks — if it does, STOP and tell the human (plan defect)

### T-5: Agent core graph  `[resolved]`
- **Objective**: The LangGraph run: classify, route, ground, compose, verify, decide, act (R2–R5, R7, R9, R11 decide-side).
- **Acceptance**: 1) graph nodes/state exactly as pinned in DESIGN 2) all model calls through LLMClient; OpenAI impl with strict structured outputs, pinned model constant 3) case facts reach drafts only via templates fed by tool results 4) verifier node scores KB drafts, threshold from config 5) gate setting respected in decide 6) graph tests with a fake LLMClient cover the four canonical scenarios end-to-end in-process 7) grounding suite: adversarial unknown-case and false-premise inputs produce escalation or refusal, never invented facts
- **Verify**: `uv run pytest -m "not live" backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/agent/**, backend/tests/graph/**, backend/tests/grounding/**`
- **Depends on**: T-1, T-2 · **parallel_safe**: false
- **Non-goals**: No checkpointing/interrupts/subgraphs; No escalation rule logic beyond calling T-6's interface (stub until T-6 lands)

### T-6: Escalation engine  `[resolved]`
- **Objective**: Hard rules + classifier + internal-note composition (R6).
- **Acceptance**: 1) hard rules exactly as pinned, deterministic, unit-tested individually 2) classifier via LLMClient emitting EscalationCall 3) final-decision combinator (rule OR classifier >= threshold) 4) internal note contains summary, grounded facts, reason enum; customer notice posted 5) wired into T-5's decide/act
- **Verify**: `uv run pytest -m "not live" backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/escalation/**, backend/tests/escalation/**, backend/src/agent/**, backend/tests/graph/**, backend/tests/grounding/**`
- **Depends on**: T-5 · **parallel_safe**: false
- **Non-goals**: No threshold tuning (T-7 owns it); No sentiment model beyond the classifier prompt

### T-7: Labeled set and escalation eval report  `[resolved]`
- **Objective**: The flagship credibility artifact — measured precision/recall on escalation (R15).
- **Acceptance**: 1) evals/labeled_set.yaml with ~50 tickets spanning all routes, all hard triggers, fuzzy frustration/complexity cases, adversarial phrasing 2) HUMAN GATE: labels reviewed and approved by the project owner before use — record approval in the fixture header; stop and ask, never self-approve 3) promptfoo run + report generator producing confusion matrix, P/R/F1, PR curve image with chosen threshold marked 4) recall >= 0.95 on the hard-trigger subset at the committed threshold; threshold written to escalation config 5) report lands in docs/eval-report/
- **Verify**: `uv run python -c "import yaml; a=yaml.safe_load(open('evals/labeled_set.yaml'))['approval']; import sys; sys.exit(0 if a.get('status')=='APPROVED' and a.get('approved_by') and a.get('approved_date') else 1)" && uv run python -m evals.report && uv run pytest -m "not live" backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `evals/**, backend/tests/evals/**, backend/src/escalation/**`
- **Depends on**: T-6 · **parallel_safe**: true
- **Non-goals**: No expanding the set past ~60; No synthetic label approval — human sign-off is external ground truth

### T-8: Portal API and approval gate  `[resolved]`
- **Objective**: Feed, draft edit/approve/reject, gate toggle, metrics (R10–R13).
- **Acceptance**: 1) endpoints exactly as pinned, X-Portal-Token auth 2) gate ON holds drafts pending; approve sends via HelpdeskPort and records gated_sent 3) metrics computed per the pinned definitions — gated sends excluded from the human-avoidance numerator 4) API tests cover both gate states and metric math
- **Verify**: `uv run pytest -m "not live" backend/tests/data backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/portal/**, backend/tests/portal/**, backend/src/data/**, backend/src/agent/**`
- **Depends on**: T-5, T-6 · **parallel_safe**: true
- **Non-goals**: No UI (T-9); No real auth/multi-user

### T-9: Portal UI  `[resolved]`
- **Objective**: The reviewer-facing React surface (R10–R12) and demo centerpiece.
- **Acceptance**: 1) feed view with route/confidence/reason/trace link 2) draft detail with editable body, approve/reject 3) gate toggle 4) metrics panel (R13) 5) builds clean; component tests for gate and edit-approve flows against a mocked API
- **Verify**: `(cd portal && npm run build && npm test)`
- **Scope**: `portal/**`
- **Depends on**: T-8 · **parallel_safe**: true
- **Non-goals**: No styling beyond clean-and-readable; No websockets — polling is fine

### T-10: Scenario runner and live e2e  `[queue]`
- **Objective**: Prove the system against real Zendesk and measure R8.
- **Acceptance**: 1) runner seeds the four canonical scenarios + adversarial unknown-case via Zendesk API (respecting rate limits) 2) asserts UI-visible effects by API read-back (reply present, note on escalation, tags, status) 3) emits latency report (p50/p95 webhook to reply) 4) p95 < 5 min against the deployed or tunneled instance 5) marked -m live, excluded from CI; requires the runbook completed by the human first
- **Verify**: `uv run pytest -m live -q`
- **Scope**: `backend/tests/live/**, scripts/scenario_runner.py`
- **Depends on**: T-4, T-6 · **parallel_safe**: true
- **Non-goals**: No load testing beyond demo volume

### T-11: Deploy, demo assets, technical documentation  `[queue]`
- **Objective**: Everything the grader touches (SPEC success criteria 5–7).
- **Acceptance**: 1) docker-compose deploy on a DigitalOcean droplet, reachable, env documented 2) docs/ assembled: architecture (+ diagram), grounding design, escalation methodology with the T-7 report, portability section (port + both adapters, naming Gorgias/Intercom/Front as future adapters), runbook 3) demo script/shot list: five live scenarios, gate flip + edited-approve on camera, metrics panel, one Langfuse trace showing tool result to templated reply 4) README quickstart verified from clean clone
- **Verify**: `uv run pytest backend/tests/deploy backend/tests/hooks -q`
- **Scope**: `docs/**, deploy/**, scripts/verify_deploy.sh, README.md`
- **Depends on**: T-3, T-7, T-9, T-10, T-17 · **parallel_safe**: false
- **Non-goals**: No video editing tooling — recording is a human step

### T-12: Scope guard matches only intended paths  `[resolved]`
- **Objective**: scope_guard.sh admits paths it should deny and denies paths it should ignore; make its matching exact and give the hook its own tests.
- **Acceptance**: 1) glob-to-regex match is anchored at BOTH ends; with scope portal/** the path backend/src/portal/routes.py is DENIED (regression test, this exact pair) 2) a path that does not resolve under CLAUDE_PROJECT_DIR exits 0 (out-of-repo scratch is never the scope guard's business), compared after realpath so ../ traversal cannot escape scope 3) a missing or empty .claude/active-ticket fails CLOSED (deny). There is no bypass token, sentinel or env var an agent can issue itself — unclaimed work is authorised by a human editing the claim record, nothing else 4) the blanket `.claude/*|docs/*` allow-everything branch is removed or narrowed to the specific paths the protocol needs (the claim record and evidence dir); today it means every ticket's docs/** and .claude/** scope declaration is unenforced, which is why scope collisions in those trees are invisible 5) backend/tests/hooks/ drives the real hook with synthetic PreToolUse JSON over a table of (ticket, path, expect) pairs covering every ticket's scope in docs/tickets.json 6) the guard's coverage gap is documented and narrowed where feasible: it is wired to Edit|Write only, so a shell redirect or `git checkout` performs an unguarded write. At minimum record this limitation in the hook header so no one mistakes the guard for a sandbox 7) .claude/evidence/ is not writable by the ticket being verified — only verify_gate.sh writes it, so a ticket cannot forge its own completion evidence
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, backend/tests/hooks/**`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No change to which paths any ticket's scope lists — this fixes the matcher, not the plan; No change to stop_guard/verify_gate session behaviour (T-13 owns that)

### T-13: Session-scoped, append-only ticket claims  `[resolved]`
- **Objective**: Guards key off a single mutable untracked file with no notion of who claimed the ticket, so a second session in the same directory is told to finish or revert another session's work.
- **Acceptance**: 1) a claim records ticket id + CLAUDE_SESSION_ID + UTC timestamp; stop_guard and verify_gate act ONLY on a claim owned by the current session 2) a second session in the same working directory is never blocked by another session's claim (fixture test asserting the observer case that fired on T-8, T-9 and T-11) 3) claim records are append-only and tracked in git, restoring the per-claim audit trail lost when .claude/active-ticket was untracked 4) guards refuse to honour a claim whose ticket already has a passing .claude/evidence/<id>.pass
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, backend/tests/hooks/**`
- **Depends on**: T-12 · **parallel_safe**: false
- **Non-goals**: No multi-agent scheduling or lock arbitration — this makes claims legible, it does not coordinate them; Worktree lifecycle (orphaned locked worktrees) is NOT fixed here — it lives outside .claude/hooks/**; raise it separately rather than widening this scope mid-ticket

### T-14: Verify commands cover their blast radius  `[resolved]`
- **Objective**: A ticket's verify runs only that ticket's own suite while its scope permits changes that break others; T-11's verify is additionally a script inside T-11's own scope.
- **Acceptance**: 1) every ticket's verify runs its own suite plus every suite that imports from its scope paths (a reverse-dependency set), or the full suite where that is simpler 2) no ticket's verify invokes a script the same ticket authors. Decidable rule for the test: tokenise the verify command, take every token that is a repo-relative path or is an argument to bash/sh, and fail if any such path matches that ticket's own scope globs. Test-runner invocations (pytest/npm/uv run pytest) with a directory argument are explicitly exempt — a ticket authoring the suite that gates it is normal TDD; a ticket authoring the gate ITSELF (the T-11 / verify_deploy.sh shape) is what this forbids 3) backend/tests/plan/ asserts both invariants above hold for EVERY ticket in docs/tickets.json, including this batch, so a future ticket cannot reintroduce either 4) docs/tickets.json gains a status field the hooks maintain, so ticket progress is readable across sessions instead of inferred from evidence files 5) docs/TASKS.md is regenerated FROM docs/tickets.json by a committed script, and a test asserts the two agree field-by-field — the mirror has already drifted on T-3, T-7 and T-8 and hand-editing is what caused it 6) the plan invariants are demonstrated failing against current docs/tickets.json before any verify command is rewritten
- **Verify**: `uv run pytest backend/tests/plan backend/tests/hooks -q`
- **Scope**: `docs/tickets.json, docs/TASKS.md, backend/tests/plan/**, scripts/render_tasks_md.py`
- **Depends on**: T-15 · **parallel_safe**: false
- **Non-goals**: No widening or narrowing of any ticket's scope globs; No retroactive re-verification of already-closed tickets; Runs after T-15 because it rewrites verify commands, including the one T-15 installs; the two otherwise contend on docs/tickets.json

### T-15: Machine-enforce the human approval gate  `[resolved]`
- **Objective**: evals.report main() returns 0 unconditionally, so T-7's verify passes green with labels no human has approved; the project's one inviolable rule is its only unenforced one.
- **Acceptance**: 1) python -m evals.report exits NON-ZERO while approval.status != APPROVED or approved_by/approved_date are empty 2) the non-zero path is reached via is_approved() only; no flag, env var or argument may bypass it — a draft render is obtained by pointing --output-dir at a scratch path, never by disabling the gate 3) tests cover both directions using a SYNTHETIC fully-approved fixture in tmp_path: real (unapproved) fixture exits non-zero, synthetic approved fixture exits zero. The real evals/labeled_set.yaml is never modified 4) the three existing not-approved tripwires keep their INTENT and still fail if a human approves (test_labels_are_not_self_approved, test_labeled_set_yaml_is_actually_not_approved_right_now, test_report_refuses_a_final_report_while_labels_are_unapproved). The single assertion inside the third that expects the report's exit code to be 0 is updated to expect non-zero as a direct consequence of acceptance 1 — that is the only permitted edit to any of the three 5) evals/REVIEW.md gains a section naming the exact assertions a human must update at approval time, so the post-approval edit is pre-authorised and legible rather than improvised 6) docs/tickets.json T-7 verify updated to assert approval
- **Verify**: `uv run pytest backend/tests/evals backend/tests/hooks -q`
- **Scope**: `evals/report.py, evals/REVIEW.md, backend/tests/evals/**, docs/tickets.json`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: NEVER sets approval.status, approved_by or approved_date, and never edits evals/labeled_set.yaml at all — this ticket exists to protect that gate, not to pass through it; Does not pre-emptively rewrite the tripwires to assert the approved state: that edit belongs to the human approval event, not to this ticket; No threshold tuning and no change to the labeled set's contents

### T-16: Test isolation and suite hygiene  `[resolved]`
- **Objective**: One shared database with per-test TRUNCATE means concurrent runs corrupt each other; the suite also rewrites a tracked file, and three conftest skip guards silently do nothing.
- **Acceptance**: 1) each pytest process gets its own Postgres SCHEMA, derived from an existing env signal (worktree path / PYTEST_XDIST_WORKER if already set / PID) with NO new dependency — pyproject.toml is T-0's scope alone and must not be edited; the schema override is readable ONLY from a test-time signal and must be inert in production — a test asserts data.db.get_connection() outside pytest uses the default schema 2) no test writes into docs/ — the unapproved-report test reads committed artefacts read-only and generates into tmp_path; git status is clean after a full run, asserted by a test 3) the pytestmark skip guards in the graph, grounding and portal conftest.py files are moved somewhere they actually take effect (a pytest_collection_modifyitems hook, or per-module pytestmark) — pytest ignores conftest-level pytestmark for sibling test modules, so today they never fire 4) the CI workflow file contains the no-skip guard and a collected-count floor, asserted by reading .github/workflows/ci.yml in a test rather than by inspection 5) two concurrent full-suite runs both pass, demonstrated by running them simultaneously and showing neither reports a truncation-induced failure
- **Verify**: `uv run pytest -m "not live" -q`
- **Scope**: `backend/tests/**, backend/src/data/**, evals/report.py, docker-compose.yml, .github/**`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No new test cases for product behaviour — this is isolation and hygiene only; No weakening of any existing assertion to make a suite parallel-safe

### T-17: Deploy verifier honours an exported DEPLOY_HOST  `[resolved]`
- **Objective**: verify_deploy.sh sources .env with set -a before reading DEPLOY_HOST, and .env defines it as an empty assignment, so an exported value is silently clobbered and the run falls to LOCAL mode while printing PASS.
- **Acceptance**: 1) DEPLOY_HOST=<host> bash scripts/verify_deploy.sh takes the REMOTE branch; an exported value is never overwritten by sourcing .env (regression test with a fake env file reproducing the clobber) 2) local mode requires an explicit opt-in flag and can never be mistaken for droplet evidence; without it, an empty DEPLOY_HOST is a hard failure rather than a silent local PASS 3) local mode is opt-in via an explicit flag and prints no PASS without it; an empty DEPLOY_HOST with no flag is a hard non-zero failure, so T-11's droplet criterion can only be met by a remote-mode run 4) docs/deploy.md and .env.example updated to match the corrected precedence
- **Verify**: `uv run pytest backend/tests/deploy -q`
- **Scope**: `scripts/verify_deploy.sh, backend/tests/deploy/**, docs/deploy.md, .env.example`
- **Depends on**: T-16 · **parallel_safe**: true
- **Non-goals**: Does not create a droplet or perform a deploy — provisioning stays a human-authorised step; No change to the deploy stack itself (deploy/**); parallel_safe only holds once T-16 lands per-process DB isolation; before that, concurrent runs share one database; NOT parallel-safe with T-11: both declare scripts/verify_deploy.sh. T-11 is currently open (blocked on the droplet), so these two must not run concurrently

### T-18: Classifier errors stop masquerading as escalations  `[resolved]`
- **Objective**: run_classifier catches bare Exception and returns None, which is the pinned abstention condition and therefore a hard escalation trigger — so any bug in that path becomes a plausible-looking escalation, silently and unlogged.
- **Acceptance**: 1) the except is narrowed to the API/timeout/parse/validation errors it actually intends to absorb; every swallow is logged with the exception type 2) a programming error raised inside the classifier path propagates instead of being converted to abstention (test asserts the exception escapes) 3) genuine abstention semantics are unchanged — a refusal or unparseable verdict still escalates, and every existing escalation test stays green 4) the three duplicated FakeLLMClient copies assert the classifier was consulted rather than only defaulting its response away, so an unanticipated call site is loud again
- **Verify**: `uv run pytest -m "not live" backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/escalation/**, backend/tests/escalation/**, backend/tests/graph/**, backend/tests/grounding/**`
- **Depends on**: T-16 · **parallel_safe**: true
- **Non-goals**: No change to the hard-rule set, the combinator, or the confidence threshold; No change to which conditions escalate — only to which FAILURES are allowed to look like them; parallel_safe only holds once T-16 lands per-process DB isolation; before that, concurrent runs share one database; NOT parallel-safe with T-7: both declare backend/src/escalation/**. T-7 is currently open (blocked on label approval), so these two must not run concurrently

### T-19: Bind the portal API contract  `[resolved]`
- **Objective**: portal/src/api.ts and backend/src/portal/schemas.py agree today purely by hand; nothing — no test, codegen step or CI job — fails if they drift, so a renamed field breaks only the live UI.
- **Acceptance**: 1) the TypeScript request/response types are GENERATED from the FastAPI OpenAPI schema by a committed script — a hand-written parity assertion is explicitly not acceptable, since re-deriving the duplication by hand is the defect 2) regenerating against the current backend produces types byte-identical to what is committed 3) a deliberate backend field rename FAILS the check (demonstrate in the test, not by hand) 4) the parity check is wired into .github/workflows/ci.yml, asserted by a test that reads the workflow file 5) the parity check is demonstrated FAILING against a deliberately renamed backend field before being wired into CI
- **Verify**: `uv run pytest -m "not live" backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q && (cd portal && npm run build && npm test)`
- **Scope**: `portal/**, backend/src/portal/**, backend/tests/portal/**, .github/**`
- **Depends on**: T-16 · **parallel_safe**: true
- **Non-goals**: No redesign of the API shape or the portal UI; No new endpoints; parallel_safe only holds once T-16 lands per-process DB isolation; before that, concurrent runs share one database

### T-20: Versioned schema migrations  `[resolved]`
- **Objective**: A single ad-hoc _MIGRATIONS string now re-executes in full on every production container start, with no record of what has been applied; it survives only because its one statement happens to be idempotent.
- **Acceptance**: 1) numbered migration files plus a schema_migrations ledger table recording what has been applied, so each migration runs exactly once 2) the existing runs.reasons column is expressed as the first migration; a database created before it still upgrades in place, and a fresh database ends in the identical schema (both proven by test) 3) container bootstrap applies only unapplied migrations rather than re-running the full list on every boot 4) a deliberately non-idempotent migration is applied exactly once across repeated init_schema calls (test) 5) a test proves a pre-existing database created WITHOUT runs.reasons is upgraded in place, run against a database built from the pre-T-8 schema
- **Verify**: `uv run pytest -m "not live" backend/tests/data backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/data/**, backend/tests/data/**, deploy/backend/**`
- **Depends on**: T-16 · **parallel_safe**: false
- **Non-goals**: No schema changes beyond formalising the existing runs.reasons migration; No ORM adoption — this is migration bookkeeping only

### T-21: Escalation eval measures the real engine  `[queue]`
- **Objective**: evals/report.py never imports EscalationEngine or run_classifier — it reimplements the precedence and fills the rest from three hand-authored replay tables, so its 1.0 scores grade a parallel implementation rather than the shipped engine. Requires a human-provided OPENAI_API_KEY and human-approved labels (T-7) before it can start.
- **Acceptance**: 1) the report calls escalation.engine.EscalationEngine.evaluate directly; STUB_CLASSIFIER_VERDICTS, STUB_STRUCTURAL_REASON and STUB_ABSTENTION_IDS are deleted, not merely bypassed 2) the classifier half runs against a live LLMClient when OPENAI_API_KEY is present; without it the report FAILS rather than silently substituting fabricated verdicts 3) a test asserts the report and the engine cannot diverge — the report has no escalation decision logic of its own 4) the recommended threshold, the sample size, and the UTC timestamp of the run that produced it are written as fields in docs/eval-report/metrics.json — not prose in report.md — so they are machine-checkable 5) recall >= 0.95 on the hard-trigger subset is measured against the real engine, per T-7 acceptance 4
- **Verify**: `uv run pytest backend/tests/evals -q`
- **Scope**: `evals/report.py, backend/tests/evals/**, docs/eval-report/**`
- **Depends on**: T-7, T-15 · **parallel_safe**: false
- **Non-goals**: NEVER edits evals/labeled_set.yaml at all, and never sets approval.status, approved_by or approved_date — same file-level prohibition as T-15, for the same reason; No relaxing of the >= 0.95 hard-trigger recall bar to make a real measurement pass; if the real engine misses it, that is a finding to report, not a number to adjust; Does not obtain or install OPENAI_API_KEY — that credential is a human-provided prerequisite, and without it this ticket cannot start

### T-22: Ticket status is maintained by the hooks, not by hand  `[resolved]`
- **Objective**: T-14 acceptance 4 specified a status field the hooks maintain; it shipped hand-maintained. Every ticket boundary since has required an out-of-scope hand edit of docs/tickets.json and docs/TASKS.md through the scope guard's documented Bash gap (observed at c237304, 8224d90, ee78825, c0aeb6b, 7c3aadd).
- **Acceptance**: 1) claim.sh sets the claimed ticket's status to in_progress and verify_gate.sh sets it to closed alongside writing .claude/evidence/<id>.pass; both regenerate docs/TASKS.md via the committed scripts/render_tasks_md.py so the sync test stays green with no agent edit 2) hook writes are surgical: a test asserts the resulting docs/tickets.json differs from the prior state by exactly one ticket's status value and nothing else 3) backend/tests/hooks drives both transitions end-to-end with synthetic events against a fixture project and asserts tickets.json and TASKS.md agree afterwards 4) backend/tests/plan/test_status_field.py's PLAN DEFECT docstring is updated to record the defect closed by this ticket; its one-directional evidence check is unchanged 5) the hook headers state the new protocol expectation: no agent-side Edit/Write of docs/tickets.json or docs/TASKS.md is needed at any ticket boundary
- **Verify**: `uv run pytest backend/tests/plan backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, backend/tests/hooks/**, backend/tests/plan/test_status_field.py`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No change to evidence semantics (T-29) or claim-format authority (T-28); Status vocabulary stays open|in_progress|closed

### T-23: T-16's outstanding proof: concurrency demonstrated, cleanliness asserted tree-wide  `[resolved]`
- **Objective**: T-16 closed with acceptance 5 (two concurrent full-suite runs both pass, demonstrated) satisfied by a sequential in-process proxy whose own docstring disclaims being the demonstration, and acceptance 2's 'git status clean after a full run, asserted by a test' reinterpreted as a docs/eval-report-only fingerprint tolerating pre-existing dirt. Its no-docs-writes test also drops the parent run's schema isolation mid-suite.
- **Acceptance**: 1) a committed test launches two genuinely concurrent subprocess pytest runs of the db-touching suites and asserts both exit 0 - re-runnable demonstration, not attestation; if runtime cost demands a representative db-heavy subset, the subset choice is justified in the test docstring 2) the post-suite cleanliness check covers the whole repo tree (git status --porcelain empty), with pre-existing dirt handled by snapshot-before-suite comparison rather than by exempting directories 3) the no-docs-writes child pytest run inherits the parent's schema isolation; a regression test reproduces the current mid-suite drop and proves it fixed 4) no existing assertion is weakened; the docs/eval-report fingerprint may be replaced only by an equal-or-stronger check
- **Verify**: `uv run pytest -m "not live" -q`
- **Scope**: `backend/tests/**`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No new product-behaviour tests; this completes T-16's own evidence; No weakening of any assertion to make concurrency pass

### T-24: Schema override is structurally test-only  `[resolved]`
- **Objective**: The per-process Postgres schema override honours its env signal wherever it appears, so a leaked env var silently switches schemas in production; inertness is convention, not structure.
- **Acceptance**: 1) the override is honoured only when an unambiguous test-context signal is also present (e.g. PYTEST_CURRENT_TEST set by pytest itself), and the gating condition is documented in db.py 2) a test spawns a fresh non-pytest interpreter with the override env var set and asserts get_connection() still uses the default schema 3) existing pytest-side schema isolation behaviour is unchanged: current isolation tests stay green
- **Verify**: `uv run pytest -m "not live" backend/tests/data backend/tests/escalation backend/tests/evals backend/tests/graph backend/tests/grounding backend/tests/ingress backend/tests/portal backend/tests/test_bootstrap.py -q`
- **Scope**: `backend/src/data/**, backend/tests/data/**`
- **Depends on**: T-23 · **parallel_safe**: false
- **Non-goals**: No change to schema naming or the orphan-schema reaper; No new dependency; pyproject.toml stays T-0's scope

### T-25: The approval gate reads only the canonical labeled set  `[resolved]`
- **Objective**: evals.report evaluates approval against whatever --labeled-set points at, so a doctored copy yields a non-draft exit-0 run without any human approval; unapproved runs also write DRAFT artifacts into docs/eval-report/ by default.
- **Acceptance**: 1) the approval decision is always evaluated against the committed evals/labeled_set.yaml regardless of any input-substitution argument; an alternate --labeled-set may drive rendering in tests but can never produce a non-draft exit-0 run 2) while unapproved, the report writes nothing under docs/: a draft render requires an explicit --output-dir outside docs/, and the default invocation exits non-zero without touching docs/eval-report/ 3) tests cover: doctored alternate file via --labeled-set still exits non-zero; a synthetic fully-approved fixture exercised without modifying the real file still exits zero, proving the gate's direction is unchanged 4) the three T-15 tripwire tests keep their intent; any assertion edit beyond what acceptance 2 directly forces is out of bounds
- **Verify**: `uv run pytest backend/tests/evals -q`
- **Scope**: `evals/report.py, backend/tests/evals/**`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: NEVER edits evals/labeled_set.yaml or its approval fields - same file-level prohibition as T-15 and T-21; No change to metrics or threshold logic; NOT parallel-safe with T-21: both declare evals/report.py; T-21 is open (human-blocked) - never run concurrently

### T-26: Plan files are tamper-evident; T-11's silent dependency edit is adjudicated  `[queue]`
- **Objective**: T-14's commit silently added T-17 to T-11's depends_on, outside its sanctioned changes; nothing detects structural edits to existing contracts. docs/INGEST.md also still describes a pre-batch world (sole root T-0, no named task list), so a literal follower fails its own confirmation step.
- **Acceptance**: 1) HUMAN GATE: the project owner ratifies or reverts T-11's depends_on addition of T-17; the decision and rationale are recorded in the completion commit message - stop and ask, never decide autonomously 2) a committed snapshot of every ticket's structural fields (scope, depends_on, verify, acceptance) plus a plan test asserting live tickets.json matches it; a legitimate amendment updates the snapshot in the same commit, making plan changes legible instead of silent 3) the snapshot test fails demonstrably against a synthetic silent depends_on edit, shown in a test using a doctored copy in tmp_path 4) docs/INGEST.md is regenerated to match reality: derives the ready set from tickets.json status and priority fields instead of asserting exactly T-0 unblocked, and names the task list (othram-support-agent) new sessions must bind to
- **Verify**: `uv run pytest backend/tests/plan backend/tests/hooks -q`
- **Scope**: `docs/tickets.json, docs/TASKS.md, docs/INGEST.md, backend/tests/plan/**`
- **Depends on**: T-22 · **parallel_safe**: false
- **Non-goals**: No re-litigating any closed ticket's contract; the snapshot pins what exists; status fields are excluded from the snapshot (T-22's hooks own them)

### T-27: Guards fail closed on every input they cannot judge  `[resolved]`
- **Objective**: scope_guard exits 0 (allow) when its python3 realpath helper fails, silently allows payloads lacking tool_input.file_path, and the Edit|Write matcher misses NotebookEdit entirely.
- **Acceptance**: 1) failure of the realpath helper (python3 absent or erroring) produces a deny naming the infra failure, not a silent allow; test simulates via PATH manipulation 2) a PreToolUse payload whose tool_input carries no file_path is denied unless the tool is on an explicit commented pathless allowlist in the hook 3) NotebookEdit is added to the settings.json matcher and its notebook_path is honoured by the guard; tests drive the real hook with NotebookEdit payloads 4) the hook header's coverage-limitation note is updated to reflect the narrowed gaps
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, .claude/settings.json, backend/tests/hooks/**`
- **Depends on**: T-22 · **parallel_safe**: false
- **Non-goals**: The Bash write path stays out of scope for the guard (documented limitation); integrity of plan files is T-26's snapshot, not a Bash sandbox

### T-28: Legacy claim lines lose their authorizing power  `[resolved]`
- **Objective**: Pre-T-13 bare claim lines still authorize: verify_gate's amnesty gates any session on an unattributed claim and stamps evidence for it, and stop_guard allows a stop regardless of evidence when the claim is legacy-shaped. The ledger's stale first line (bare T-13) keeps the format alive.
- **Acceptance**: 1) verify_gate refuses to run a gate or write evidence for a claim record with no session attribution; the refusal names the offending record 2) stop_guard treats a legacy bare line as inert history: it neither blocks nor authorizes; current-claim resolution considers only JSON records 3) pre-authorized test edits, and ONLY these, per the justify-test-edit rule: test_legacy_claim_line_allows_regardless_of_evidence (stop_guard) and the verify_gate amnesty tests are rewritten to assert the new fail-closed behaviour with the same fixtures 4) the stale bare first line of .claude/active-ticket is retired via a sanctioned migration (JSON tombstone record or a dedicated migration commit); raw Edit/Write rewrites of the ledger remain denied 5) scope_guard may keep reading legacy lines for scope decisions (the 113 pre-T-13 scope tests stay valid); only gating and stopping authority is withdrawn
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, backend/tests/hooks/**`
- **Depends on**: T-27 · **parallel_safe**: false
- **Non-goals**: No change to the append-only enforcement itself; No multi-session arbitration beyond what T-13 established

### T-29: Evidence binds to the tree it certifies  `[queue]`
- **Objective**: A .pass file is a bare epoch; nothing ties it to a commit, so a completion-titled commit and the tree the gate actually verified can diverge. Observed: T-12's gate closed 33 minutes and one commit after its completion-titled commit; only timestamp forensics could reconstruct which tree passed.
- **Acceptance**: 1) verify_gate records epoch, HEAD commit hash, and a dirty-tree flag as single-line JSON in .claude/evidence/<id>.pass; a fixture without git is recorded as such rather than crashing the gate 2) claim refusal (T-13 acceptance 4) and stop_guard parse both formats during migration: a bare-epoch legacy file is honoured for already-closed tickets but never newly written 3) hooks tests assert the recorded hash equals the fixture repo's HEAD at gate time and that a dirty tree is flagged 4) the hook header documents what the binding proves and does not prove: it certifies the tree the verify ran on, not that that tree was committed
- **Verify**: `uv run pytest backend/tests/hooks -q`
- **Scope**: `.claude/hooks/**, backend/tests/hooks/**`
- **Depends on**: T-28 · **parallel_safe**: false
- **Non-goals**: No retroactive rewriting of existing evidence files; No signing or cryptographic chain - commit binding only

### T-30: Close the audit's low-severity proof gaps  `[resolved]`
- **Objective**: Three verified-low gaps from the batch audits: escalation/rules.py's abstention docstring still describes pre-T-18 semantics; the migration convergence test compares only name/type/nullability so a divergent column default or array element type would pass; discover_migrations returns an empty list silently when the migrations directory is missing from an image.
- **Acceptance**: 1) rules.py's abstention docstring matches shipped semantics: narrowed absorb-set, logged swallows, programming errors propagate 2) the schema-convergence test also compares column defaults and array element types (udt_name), and fails demonstrably against a synthetic divergence via a doctored migration in tmp fixtures 3) a missing migrations directory aborts schema init loudly instead of booting with zero migrations; a test simulates the stripped-image case and asserts the loud failure
- **Verify**: `uv run pytest -m "not live" -q`
- **Scope**: `backend/src/escalation/rules.py, backend/src/data/**, backend/tests/data/**`
- **Depends on**: T-24 · **parallel_safe**: false
- **Non-goals**: Docstring-only change in escalation: no behaviour edits outside data/; No change to which exceptions the classifier absorbs (T-18 settled that)

### T-31: Complete the harness-sync migration and preserve auditable closure history  `[resolved]`
- **Objective**: The harness-sync commit removed the production claim parser, verification hook, and active-ticket ledger while retaining their contracts and tests; it also moved historical receipts to evidence-v1 although the new lifecycle recognizes only JSON receipts. The non-live suite is broken and historical closures now resolve as queued.
- **Acceptance**: 1) The active lifecycle, configured hooks, and hook tests agree on one supported claim/close protocol; the hook test suite passes without merely deleting coverage for ownership, close gating, or fail-closed behavior 2) Historical evidence-v1/*.pass is reconciled under an explicit migration policy: retain it as clearly labeled non-auditable legacy closure records that the lifecycle can recognize, or restore/remint receipt-bound records; the implementation must not fabricate commit hashes or fingerprints from historical bare timestamps 3) Every plan ticket marked closed has a lifecycle status consistent with the chosen migration policy; dependencies are not silently regressed to queue solely by the sync 4) Regression tests prove both a legacy closure record and a new fingerprint-bound JSON receipt behave as specified, and the full non-live suite passes
- **Verify**: `uv run pytest -m "not live" -q`
- **Scope**: `.claude/hooks/**, .claude/scripts/**, .claude/evidence-v1/**, .claude/evidence/**, .claude/settings.json, backend/tests/hooks/**, backend/tests/plan/**`
- **Depends on**: none · **parallel_safe**: false
- **Non-goals**: No fabricated historical commit or fingerprint metadata; No changes to product behavior outside the harness and its tests
