# cxforge — build plan to the finish line

Written 2026-08-16. Supersedes `docs/tickets.json` for all remaining work (ADR-001).
Read `docs/STATE.md` first for what is actually true today, and `docs/DECISIONS.md` for
why each choice below was made.

**Governing constraint:** the schedule is compressed by running independent tracks
concurrently, *not* by reducing verification (ADR-016). Every work package below ships.

**Hard calendar limit:** the Zendesk trial lapses around **2026-08-27**. Wave 4 must
complete before then or the trial has to be recreated from `docs/zendesk-runbook.md`.

---

## 0. Critical path in one line

```
W0 Foundation ──► W1-A Core loop ──► W2 (B ‖ C) ──► W3 Integration ──► W4 Live e2e ──► W5 Demo
                       ▲                                                    ▲
   W1-D Portal ────────┘  (independent)              owner actions ─────────┘
   W1-E Eval tooling ──┘  (independent)              (Zendesk / Cloudflare)
   W1-F Infra ─────────┘  (builds to frozen contract)
```

Nothing downstream of W1-A can be demonstrated until W1-A lands. It is the only true
blocker; everything else is either parallel to it or downstream of it.

---

## 1. Contracts — frozen before Wave 1 starts

Parallel tracks build against these. They are fixed in W0.3 and must not be renegotiated
mid-wave; if one is wrong, stop and change it deliberately in `docs/DESIGN.md` first.

### 1.1 Job payload (ADR-002)

```python
# backend/src/worker/jobs.py
class TicketJob(BaseModel):
    ticket_id: str
    comment_id: str
    received_at: datetime      # UTC, stamped in the ingress handler — ADR-004
```

arq task name: `run_ticket`. Queue name: `cxforge:jobs`. **Redis URL env var: `REDIS_URL`**
— added to the frozen set 2026-08-16. §1.1 originally omitted it; Tracks A and F picked the
same name independently and agree, but nothing froze it, so a rename on either side would
diverge silently. The worker container's command is **`arq worker.main.WorkerSettings`**,
also pinned after the fact for the same reason. The handler enqueues **after**
the `tickets_seen` insert succeeds and **only** when `is_new` is true. The endpoint keeps
`status_code=202` and its existing response body unchanged — a failed run never changes
what Zendesk sees.

### 1.2 `run_agent` gains an injected clock (ADR-004)

```python
def run_agent(
    ticket_id: str,
    *,
    port: HelpdeskPort,
    llm: LLMClient,
    escalation_decider: EscalationDecider | None = None,
    received_at: datetime | None = None,     # NEW
) -> RunState: ...
```

`backend/src/agent/nodes.py:591` currently reads `received_at = datetime.now(UTC)`. It
becomes: use the injected value when present, else `datetime.now(UTC)`. The fallback is
what keeps all 78 existing graph/grounding/**escalation** tests passing unchanged.

> Counted 2026-08-16, because the number was challenged in verification: `graph` collects
> 11 and `grounding` 11 — **22** together, not 78. Adding `escalation` (56) gives exactly
> **78**, which is also how `docs/STATE.md §6.1` phrases it. The figure was right and the
> label had dropped a directory. Track A's acceptance is the 78.

### 1.3 Retrieval relevance floor (ADR-010)

```python
def search_kb(
    query: str, k: int = 5, *, embedder: Embedder | None = None,
    min_score: float | None = None,          # NEW; defaults to config KB_MIN_SCORE
) -> list[RetrievedChunk]: ...
```

Chunks scoring below the floor are dropped. An empty result is what finally makes R6's
`empty_retrieval` hard trigger reachable. The floor is calibrated against **Voyage**
scores after the reseed — hashing-era numbers do not transfer.

### 1.4 Embedder (ADR-008)

The `Embedder` Protocol (`dim: int`, `embed(texts) -> list[list[float]]`) is already the
right seam and does not change. Add `VoyageEmbedder` alongside `HashingEmbedder`.

> **Use `voyage-4-lite` with `output_dimension=1024`** — verified against Voyage's model
> reference on 2026-08-16. The `voyage-4` line supports configurable output dimensions
> (256 / 512 / 1024 / 2048), and pinning 1024 matches the current `EMBEDDING_DIM = 1024`
> and the `kb_chunks.embedding vector(1024)` column exactly: **reseed only, no schema
> migration.**
>
> Do not use `voyage-3` (legacy, fixed 1024) or `voyage-3-lite` (legacy, fixed 512 — would
> force a column change). Pass `output_dimension` explicitly rather than relying on the
> default, so the contract is visible at the call site.

`HashingEmbedder` stays in the tree as the offline default so CI and the non-live suite
keep running with no network and no key.

### 1.5 Customer history (ADR-009)

```python
# backend/src/helpdesk/port.py  — the T-2/T-3 boundary. This is the sign-off (ADR-009).
class HelpdeskPort(Protocol):
    ...
    def fetch_requester_history(
        self, requester_email: str, *, exclude_ticket_id: str, limit: int = 5
    ) -> list[TicketSummary]: ...
```

`TicketSummary` is a new normalized model in `helpdesk/models.py`:
`(id, subject, status, created_at, tags)`. Both adapters implement it; the parametrized
contract suite covers it for both (R14).

### 1.6 Tracing (ADR-006)

Spans are keyed on the **`trace_id` already minted in `act`** — do not mint a second one.
`portal/service.py::_trace_url` keeps its URL shape and finally resolves.

### 1.7 What does *not* change

`tickets_seen` keeps its two-column shape — receipt time rides on the job payload, not the
table (ADR-003 releases the row on failure rather than tracking state on it). The
`runs.received_at` / `replied_at` columns already exist; only their *meaning* is corrected.
The pinned `202` and the **8 `== 202` assertions across 6 tests** in
`backend/tests/ingress/test_webhook.py` are preserved. (Earlier revisions of this line said
"the 8 ingress tests", conflating assertions with tests — counted 2026-08-16.)

---

## 2. Wave 0 — Foundation (serial, blocks everything)

| ID | Work | Files |
|---|---|---|
| **W0.1** | Clear the two stray `* 2.py` files by **moving** them to the scratchpad (reversible; they are untracked). Re-baseline: full suite, ruff, mypy — record the numbers. | `.claude/scripts/harness_lib 2.py`, `backend/tests/hooks/test_close_unattributed_claim_gap 2.py` |
| **W0.2** | Retire the harness (ADR-001). Write `.claude/rules/build-protocol.md`; repoint `CLAUDE.md`; disable the `scope_guard` / `stop_guard` / `task_gate` hooks in `.claude/settings.json` (keep `heartbeat`). Leave `.claude/evidence/` **untouched**. | `.claude/rules/**`, `CLAUDE.md`, `.claude/settings.json` |
| **W0.3** | Amend `docs/SPEC.md` and `docs/DESIGN.md`: Anthropic provider, the §1 contracts above, promptfoo/DeepEval status, customer history in scope. Cross-link `docs/DECISIONS.md`. | `docs/SPEC.md`, `docs/DESIGN.md` |

**Acceptance:** pytest collects 7 fewer tests than before W0.1; `git status` is clean apart
from intended changes; suite/ruff/mypy numbers recorded in the commit body; §1 contracts
appear verbatim in `docs/DESIGN.md`.

**Why W0.1 matters:** the duplicate test module is a pre-fix revision that asserts the
**opposite** of current behaviour (expects `returncode == 0` and a minted receipt where
`cmd_close` now returns 1 and mints nothing). pytest collects it by default glob.

---

## 3. Wave 1 — four tracks in parallel

### Track A — The core loop ★ critical path

| ID | Work | Files |
|---|---|---|
| A1 | Add `redis` + `arq` deps. Create `backend/src/worker/` — `jobs.py` (the `TicketJob` model), `main.py` (arq `WorkerSettings`), `settings.py` (Redis URL from env). | `pyproject.toml`, `backend/src/worker/**` |
| A2 | Ingress enqueues `run_ticket` after a successful, non-duplicate `tickets_seen` insert, stamping `received_at`. Response body and `202` unchanged. | `backend/src/ingress/__init__.py` |
| A3 | Worker handler builds `ZendeskAdapter()` + `AnthropicLLMClient()` and calls `run_agent(..., received_at=job.received_at)`. | `backend/src/worker/main.py` |
| A4 | Failure semantics (ADR-003): on any exception, `DELETE FROM tickets_seen WHERE ticket_id=%s AND comment_id=%s`, then log at ERROR with ticket id and exception. | `backend/src/worker/main.py` |
| A5 | Thread `received_at` through `run_agent` → graph config → `act`, replacing `datetime.now(UTC)` at `nodes.py:591` with the injected value (fallback preserved). | `backend/src/agent/graph.py`, `backend/src/agent/nodes.py` |
| A6 | Correct the three finger-pointing docstrings that assert the omission is deliberate. | `backend/src/ingress/__init__.py:22-23`, `backend/src/portal/deps.py:17-20`, `docs/architecture.md:81` |
| A7 | Tests. **All of them go in `backend/tests/ingress/` — do NOT create `backend/tests/worker/`.** See the blast-radius note below. | `backend/tests/ingress/**` |

**A7 must prove, at minimum:**
1. A valid webhook **enqueues** and does **not** run inline (assert on the queue, and that
   `run_agent` was not called in-process).
2. The worker consuming that job calls `run_agent` with the ticket id **and the job's
   `received_at`**, not a fresh clock.
3. A run that raises **deletes** the `tickets_seen` row, and a subsequent identical
   webhook is treated as new.
4. The `202` contract and existing response body are byte-identical to before.
5. `runs.received_at` equals the webhook stamp — assert the *interval*, not just presence,
   by driving a run with a known past timestamp and checking `replied_at - received_at`
   spans the whole run.
6. A duplicate webhook enqueues **nothing**.

**Sabotage check before declaring A done:** break the enqueue line and confirm A7.1 fails.
A test that passes with the wiring removed is not testing the wiring.

> **Blast-radius trap — corrected 2026-08-16, before Track A started.** This row used to
> say `backend/tests/worker/**`. Creating that directory turns **4–11 tickets red** in
> `backend/tests/plan/test_blast_radius.py`, most of them closed. `_planlib.py:161-174`
> maps `backend/tests/<dir>/**` and a bare `backend/tests/<file>.py` to the **same** kind
> of graph node, so a subdirectory is not a workaround — it is the identical failure. The
> only exemption is the root `backend/tests/conftest.py`. Cost by import: `data` → 4
> tickets (T-1, T-8, T-20, T-24); `ingress` → 5 (adds T-4); **`from main import app` → 11**,
> because `main` fans out to `ingress` + `portal` and thence to `agent`, `escalation`,
> `helpdesk`, `data`. A `conftest.py` *inside* `worker/` does not dodge it; only relative
> imports are invisible to the graph.
>
> `backend/tests/ingress/` and `backend/tests/portal/` are the **only** two suite dirs
> whose transitive closure already contains `data` + `main`, so adding files there adds no
> edge and costs **0 failures**. Put every A7 test in `backend/tests/ingress/`.
>
> `backend/src/worker/` itself is **safe** — `worker` is in neither `FIRST_PARTY_ROOTS`
> (`_planlib.py:92-101`) nor `KNOWN_PACKAGES` (`:239`), so it trips nothing. That is also
> the catch: the blast-radius graph will silently under-report everything depending on
> `worker`. Recorded as an open question in §10 rather than fixed here, because adding
> `worker` to those lists turns tickets red on its own and is a deliberate change.
>
> Also true regardless of directory: `backend/tests/conftest.py:346-401` diffs
> `git status --porcelain` across the session and fails the run if a test writes any path
> outside the harness allowlist — new tests must write only under `tmp_path`.

### Track D — Portal wireframe + stylesheet (zero dependencies — start immediately)

| ID | Work | Files |
|---|---|---|
| D1 | Semantic markup + stable class hooks across `App`, `Feed`, `DraftDetail`, `GateToggle`, `MetricsPanel`. Structure is the deliverable (ADR-012). | `portal/src/**` |
| D2 | One minimal stylesheet — system font stack, spacing, readable table, clear toggle and status badges. Small and easy to discard. | `portal/src/styles.css` |
| D3 | **Bug:** render `run.outcome`. Escalated and off_topic runs currently display as "auto-sent". | `portal/src/components/Feed.tsx` |
| D4 | **Bug:** `App.tsx` selection lookup matches `draft_id === null` when nothing is selected. | `portal/src/App.tsx` |
| D5 | Empty-state and loading copy that reads deliberately rather than as a failure. | `portal/src/components/**` |

**Acceptance:** `cd portal && npm run build && npm test` green (5 vitest tests kept, plus
new ones for D3/D4); a stylesheet asset appears in the built bundle; no API contract change.

### Track E — Eval tooling (zero dependencies)

| ID | Work | Files |
|---|---|---|
| E1 | Build a real promptfoo suite over the canonical scenarios and the adversarial grounding set. Install promptfoo; replace the `tests: []` scaffold. | `promptfooconfig.yaml`, `evals/promptfoo/**` |
| E2 | Settle DeepEval: make it do real work in the grounding suite, or remove it from `pyproject.toml` and from SPEC. Do not leave it as an unused dependency. | `pyproject.toml`, `backend/tests/grounding/**` |
| E3 | Route-classification accuracy harness over the existing 51-ticket labeled set (`expected_route` is already present) against the live model. This is the biggest unmeasured risk before filming. | `evals/route_accuracy.py`, `backend/tests/evals/**` |

**Acceptance:** `npx promptfoo eval` runs and asserts something that fails when the prompt
is degraded; a route-accuracy number exists with a per-route confusion matrix.

### Track F — Infrastructure (builds to the §1 contract, not to A's code)

| ID | Work | Files |
|---|---|---|
| F1 | Add `redis` and `worker` services to both compose files. Worker runs the same image with an arq CMD. | `docker-compose.yml`, `deploy/docker-compose.yml` |
| F2 | `cloudflared` named tunnel (ADR-005) as a supervised service, config committed, token from env. | `deploy/**` |
| F3 | **Env-forwarding audit.** Assert every variable the app reads is forwarded into every container — a test, not a checklist. This is the exact class of defect that let the stack run for weeks with no `ANTHROPIC_API_KEY`. | `backend/tests/deploy/**`, `deploy/docker-compose.yml`, `.env.example` |
| F4 | Fix the `.env` loading gap (`STATE.md §6.14`): nothing calls `load_dotenv()`, so documented commands silently see no credentials. | `backend/src/main.py` or the documented commands — pick one and make it consistent |

---

## 4. Wave 2 — after Track A merges (both tracks touch `agent/nodes.py`)

Run **B and C concurrently**, but only after A is on `master`. See the ownership matrix
in **§8** — they touch disjoint functions within `nodes.py`.

### Track B — Retrieval and agent quality

| ID | Work | Files |
|---|---|---|
| B1 | `VoyageEmbedder` — **`voyage-4-lite`, `output_dimension=1024` passed explicitly** (§1.4; no schema change). Reseed the KB. Verify retrieval quality against known-good queries; do not assume it improved. | `backend/src/data/embeddings.py`, `backend/src/data/seed.py` |
| B2 | Relevance floor (§1.3) + `KB_MIN_SCORE` config, calibrated on Voyage scores. Add a test proving R6's `empty_retrieval` trigger now **fires**. | `backend/src/data/retrieval.py`, `backend/src/agent/config.py` |
| B3 | Permission-route rewording (ADR-011). | `backend/src/agent/templates.py` |
| B4 | Customer history (§1.5): port method, both adapters, contract suite, and prior-ticket context into the classifier prompt. | `backend/src/helpdesk/**`, `backend/src/agent/nodes.py` (classify), `backend/tests/contract/**` |

**Acceptance:** the contract suite passes for **both** adapters (R14 preserved); a test
demonstrates an escalation caused by empty retrieval; a test demonstrates history reaching
the classifier.

### Track C — Observability

| ID | Work | Files |
|---|---|---|
| C1 | Langfuse instrumentation (ADR-006): spans for `classify`/`compose`/`verify` under the `trace_id` `act` mints. Must degrade to a no-op when keys are absent so the offline suite stays offline. | `backend/src/agent/llm.py`, `backend/src/agent/nodes.py` (act) |
| C2 | Structured logging + configuration. Today: two `logger.warning` calls and no logging config anywhere. | `backend/src/main.py`, `backend/src/worker/main.py` |
| C3 | Metrics honesty: add `sample_count` to `MetricsResponse`; return `null` rather than `0.0` for percentiles over an empty sample. Delete the false docstring at `portal/service.py:307-311`. | `backend/src/portal/service.py`, `portal/src/components/MetricsPanel.tsx` |

**Acceptance:** a real run produces a Langfuse trace whose spans show the `Case` tool
result feeding the templated reply; the portal trace link resolves; `/api/metrics` on an
empty database does **not** report a passing p95.

---

## 5. Wave 3 — Integration

| ID | Work | Depends on |
|---|---|---|
| G1 | Regenerate the eval report live with model identifier + threshold sweep; add the R15 basis statement (10 of 16 hard triggers) next to the headline recall. | B (embeddings affect retrieval), E |
| G2 | **Deep deploy check:** extend `verify_deploy.sh` to POST a signed synthetic webhook and assert a `runs` row appears. It will fail until A is deployed — that is the point. | A, F |
| G3 | Redeploy the droplet with redis + worker + cloudflared. Verify by reading effects back, not by trusting the command. Remember `set -a; source .env; set +a` **before** `docker compose up` (`docs/deploy.md:139`) or every `${VAR}` falls back to its compose default. | F, owner OA2 |

---

## 6. Wave 4 — Live e2e (T-10's real deliverables) — gated on owner actions

| ID | Work | Files |
|---|---|---|
| H1 | `scripts/scenario_runner.py`: seed **20–30** tickets covering the four canonical scenarios + the adversarial unknown-case, respecting rate limits, re-runnable without exhausting the trial. | `scripts/scenario_runner.py` |
| H2 | Assert UI-visible effects by API read-back: reply present, internal note on escalation, tags, status. | `backend/tests/live/**` |
| H3 | Latency report: p50/p95 **webhook → public reply**, measured both externally (runner stopwatch) and from `/api/metrics`, and shown to agree. Assert p95 < 5 min (R8). | `scripts/scenario_runner.py` |

**Acceptance:** SPEC success criterion 1 demonstrated against the **deployed** droplet;
success criterion 6 backed by 20–30 real data points rather than a vacuous zero.

> **Status 2026-08-17 — criterion 1 is met; criterion 6 is partly met.** A real Zendesk comment
> drove a full run to a publicly posted reply on the droplet, and `/api/metrics` reports
> `sample_count: 3` with p50 `15.0s` / p95 `15.3s` (§10.6a–b). What remains for this wave is the
> **volume**: H1's 20–30-ticket runner, H2's read-back assertions across the canonical scenarios,
> and H3's external-vs-`/api/metrics` agreement. Three data points are real evidence, not the
> 20–30 ADR-015 commits to. The prerequisites §10.5(g) named — the tunnel, the trigger, a live
> credential, and two working loop guards — are all now satisfied.

---

## 7. Wave 5 — Documentation and demo

| ID | Work |
|---|---|
| J1 | **Rewrite `README.md`.** It is the first thing a grader reads and currently wrong on at least eight counts (`STATE.md §6.12`). Include the missing `VITE_PORTAL_TOKEN` step and verify the quickstart from an actual clean clone. |
| J2 | Refresh `architecture.md` (new worker/queue topology + diagram), `grounding.md`, `escalation.md` (customer history), `deploy.md` (redis/worker/cloudflared), `portability.md`, `zendesk-runbook.md` (named-tunnel flow). |
| J3 | Rewrite `demo-script.md` against what is now actually recordable, and re-verify each shot. **Two known deltas from W1-D:** the feed's filter dropdown now shows prose labels (`sent, no review`) instead of raw enum values (`auto_sent`) — the `value` attributes and the `GET /api/feed?status=` call are unchanged, but any shot that tells the operator to pick "auto_sent" names a string that is no longer on screen. And the feed now has **two** status columns (Outcome + Draft) where the script may describe one. |
| J4 | **Record the demo video** — owner action. |

---

## 8. File-ownership matrix (keeps concurrent tracks off each other)

| File / area | W1-A | W1-D | W1-E | W1-F | W2-B | W2-C |
|---|---|---|---|---|---|---|
| `backend/src/ingress/**` | ● | | | | | |
| `backend/src/worker/**` | ● | | | | | ○ (C2 logging) |
| `backend/src/agent/graph.py` | ● | | | | | |
| `backend/src/agent/nodes.py` | ● `act` | | | | ● `classify` | ● `act` spans |
| `backend/src/agent/templates.py` | | | | | ● | |
| `backend/src/agent/llm.py` | | | | | | ● |
| `backend/src/data/**` | | | | | ● | |
| `backend/src/helpdesk/**` | | | | | ● | |
| `backend/src/portal/**` | | | | | | ● |
| `portal/src/**` | | ● | | | | ○ (C3 panel) |
| `evals/**`, `promptfooconfig.yaml` | | | ● | | | |
| `deploy/**`, compose files | | | | ● | | |

`backend/src/agent/nodes.py` is the one genuinely contended file. **A owns it in Wave 1
and must land first.** In Wave 2, B touches `classify` and C touches `act`'s span
wrapping — disjoint functions, but rebase rather than merge, and run the full suite after.

---

## 9. Definition of done — per work package

A package is done when **all** hold. No exceptions, no "green enough" (ADR-016).

1. `uv run pytest -m "not live" -q` passes at or above the W0.1 baseline count.
2. `uv run ruff check .` clean — **and** `backend/src/portal` / `backend/tests/portal`
   linted explicitly, because `extend-exclude = ["portal", ...]` is gitignore-style and
   silently skips any directory named `portal` at any depth.
3. `uv run mypy backend` clean.
4. `cd portal && npm run build && npm test` green when `portal/**` changed.
5. **The new behaviour has a test that fails when the new code is removed.** Verify this
   by actually removing it, not by reasoning about it.
6. Anything claimed as working against a live system was **read back** from that system,
   not inferred from a command's exit code.

---

## 10. Blocked-on-owner summary

See `docs/OWNER-ACTIONS.md` for exact commands. Ordered by what they unblock:

Status as of **2026-08-17**, each verified by reading the effect back — not by being told:

| Action | Gates | Status |
|---|---|---|
| Voyage AI API key | W2-B1 | ✅ **DONE** — key present and verified live 2026-08-16 (`voyage-4-lite`, `output_dimension=1024` → exactly 1024 dims). **But the account has no payment method**, so it is on the free tier's 3 RPM / 10k TPM — see §10.3 |
| Voyage billing + `KB_EMBEDDER=voyage` in the deploy env | the Voyage reseed being what production actually uses | ❌ **OUTSTANDING** — see §10.3 |
| Langfuse `cxforge` project + correct `pk-lf-` / `sk-lf-` pair | W2-C1 | ✅ **DONE** — keys resolve to project `cxforge`; prefixes differ |
| Cloudflare domain + named tunnel token | W1-F2, W3-G3, W4 | ✅ **DONE — verified 2026-08-17.** Service repointed to **`portal:80`** (not `backend:8000`); `https://cxforge.hankholcomb.com` serves `/` 200, `/health` 200, `/api/*` 401, `/webhooks/zendesk` 401 unsigned, no droplet port exposed. The webhook URL did not change and a valid HMAC signature survives the nginx proxy. See §10.6 and OA-3 |
| Zendesk OAuth re-auth (browser PKCE consent) | **W3-G3, W3-G2's ability to pass, W4 entirely** | ✅ **SOLVED — 2026-08-17, and it was never a recurring chore.** A refresh token was being issued all along; the old script discarded it. Renewal is `uv run python scripts/zendesk_oauth.py --refresh`, **no browser**, proven twice. Browser consent only if the 30-day refresh token lapses. One limitation remains (rotation does not survive a container restart) — §10.7(a) and OA-4 |
| Record the demo video | W5-J4 | Last |

### 10.1 ✅ RESOLVED — the retired harness no longer constrains new work

**Surfaced 2026-08-16 while scoping Track A. Decided the same day: `docs/DECISIONS.md`
ADR-018.** `backend/tests/plan/**` — all 92 tests — moved to
`.claude/harness-archive/plan-tests/`, preserved and no longer collected. Both questions
below are answered: question 2 is "no, they are retired", and question 1 dissolves, because
there is no longer an import graph to be blind about `worker`.

The rule those tests enforced is **subsumed by the gate**: `test_blast_radius.py` existed so
a ticket's *narrow* verify command covered everything its scope could break, and ADR-001
replaced narrow verifies with the full suite before every commit. No product coverage was
lost. Suite drops ~92; CI's floor of 200 is untouched. `backend/tests/hooks/**` was the same
category and was left alone *here* — **superseded 2026-08-17**: it is now
`.claude/harness-archive/hooks-tests/` as well, per the amendment at the end of
`docs/DECISIONS.md` ADR-019 (the trigger was CI failing 30 of 30 runs on a test that was
wrong about Linux, not the guard being wrong).

<details><summary>The original open question, kept for the record</summary>

ADR-001 retires the ticket harness and declares `docs/tickets.json` historical. But
`backend/tests/plan/**` — seven test modules, including `test_blast_radius.py` — is still
in the gated suite and still gates **on `docs/tickets.json`, unfiltered by status**
(`test_blast_radius.py:25`, `_planlib.py:47-49`). So closed tickets' contracts now dictate
where *new* engineering may put its files. Track A hit this immediately: the plan's own
`backend/tests/worker/**` would have turned up to 11 closed tickets red.

The workaround costs nothing (put the tests in `backend/tests/ingress/`), so nothing is
blocked. Two things the owner may want to decide later:

1. **Does `worker` join `FIRST_PARTY_ROOTS`/`KNOWN_PACKAGES`?** Today it is invisible to
   the import graph, so blast-radius silently under-reports every dependency on the new
   worker package. Adding it restores the signal but turns tickets red by itself.
2. **Do the `backend/tests/plan/**` suites survive the harness that they encode?** They are
   real, passing tests, so `.claude/rules/build-protocol.md` rule 7 says they are not to be
   weakened to make new work convenient. But they encode a lifecycle ADR-001 ended. Either
   answer is defensible; neither should be reached by a subagent quietly editing a closed
   ticket's `verify` string.

</details>

### 10.2 Measured against the live model for the first time — two real gaps

**W1-E3, 2026-08-16.** `evals/route_accuracy.py` drove the shipped `agent.nodes.classify`
against `claude-opus-5` over all 51 labeled tickets (51 calls, $0.30). This is the first time
anything in this repo measured route classification — `STATE.md §6.1` records that every
canonical test hands the route in via a canned `Classification`.

**Headline: route accuracy 1.000 (30/30)** on the four branch routes, P=R=F1=1.0 each, zero
misroutes. R2–R5's happy paths are safe to film. *Same caveat as §4.2 applies:* 1.000 over 30
tickets whose labels and whose prompt were authored in the same build says the scenarios are
internally consistent, not that the model is robust to real customer prose.

**Gap 1 — two escalation triggers do not fire, because the ticket lands on the wrong branch.**
`classify` structurally cannot emit `escalate`; 8 of the 21 escalate tickets are
*route-dependent*, meaning the escalation condition is detected inside one specific branch
node. Route-dependent accuracy is **6/8 (0.75)**:

| ticket | needs | got | confidence |
|---|---|---|---|
| `esc-low_confidence-verifier_failure-exact-date-01` | `kb` | `case_status` | 0.92 |
| `esc-low_confidence-verifier_failure-summed-timeline-01` | `kb` | `case_status` | 0.92 |

*"Can you tell me the EXACT calendar date my results will be ready?"* and *"Add up every
remaining stage — exactly how many total days?"* are classified as case-status questions with
high confidence, and that reading is defensible. But the labels expect them to reach `verify`
and fail groundedness. **In production against a resolvable case they get a templated ETA
reply instead of escalating** — a live R6 gap, reproduced independently by promptfoo.

Fixing it means either rewording `CLASSIFY_SYSTEM` (Track B owns prompt wording) or
re-examining those two labels. **Owner decision, not a subagent's.**

> ### ✅ Gap 1 RESOLVED — `docs/DECISIONS.md` **ADR-020**, 2026-08-16, implemented in W2-B5
>
> **The owner decided the model is right and the labels were wrong.** Both tickets are now
> ordinary `case_status` labels in `evals/labeled_set.yaml`, each carrying a `relabeled:`
> block with the measurement, the reasoning and the consequence. `CLASSIFY_SYSTEM` was
> **not** reworded — bending the classifier away from a reading the owner agrees is correct
> would be fixing the measurement instead of the thing measured.
>
> The real worry — answering an exact-date question with unearned precision — is addressed
> where it belongs, in the reply: `render_case_status_reply` now ends its estimate with
> *"— an estimated timeline, and subject to change."*, scoped to replies that actually state a
> timeline, and stating no number, stage or cause so it cannot trip `grounding_guard`.
>
> **Three consequences, recorded so nobody rediscovers them as bugs:**
>
> 1. **The published eval numbers will move.** These two tickets sit in the escalation set
>    behind `docs/eval-report/`'s `P = R = F1 = 1.000` and its hard-trigger recall, and
>    were two of the six `evals/report.py` excludes as structurally unmeasurable — so the
>    denominator *and* `measured_sample_size` both change. ADR-007 already commits to
>    regenerating live and publishing whatever the numbers are; that is **Wave 3 G1**.
>    Nothing under `docs/eval-report/` was touched.
> 2. **The labeled set is now 52 tickets — 32 branch-route, 20 escalate** (was 51/30/21).
>    Both relabeled tickets were the set's only `verifier_failure` examples, so one
>    replacement was written (`esc-low_confidence-verifier_failure-summed-stages-01`,
>    the same trap posed about the *published* stage windows with no case in it) rather
>    than weakening `test_every_low_confidence_subtype_is_covered`.
>    `backend/tests/evals/test_route_accuracy.py`'s pinned counts were re-derived.
> 3. **`evals/route_accuracy.py` needed a real port.** W2-B4 gave `classify` a second
>    collaborator (`fetch_requester_history`, ADR-009), and that harness had deliberately
>    filled the `port` slot with a raise-on-touch sentinel. It did its job — it failed
>    loudly — and now gets `_NoHistoryPort`, which answers "no prior contact" and still
>    raises on every other method. Anything else that drives `classify` directly needs the
>    same one-line change.
>
> Gap 2 below is untouched and still open.

**Gap 2 — `grounding_guard` false-positives on correct refusals.** On 3 of 4 adversarial
kb-route cases the live model answers *correctly* — refuses plainly, states it has no case
access, does not affirm the false premise — and the shipped guard flags it anyway, because
the reply quotes the customer's own case id back, or places a general five-stage explanation
next to the words "your case". These are the deliberate false positives `grounding_guard`'s
docstring calls "the intended failure mode", now measured for the first time.
**Operationally: an adversarial case-status question that lands on the kb route escalates in
production.** Safe (it escalates rather than fabricates) but it inflates the escalation rate,
which is a demo-visible metric.

### 10.3 W2-B measured findings — two owner actions the reseed surfaced

**2026-08-16, W2-B1/B2.** Both measured live, neither assumed.

**(a) Nothing runs on Voyage until `KB_EMBEDDER=voyage` is set in the deploy env — and the
default is the *lexical* embedder.** `HashingEmbedder` stays the default so CI and the
`-m "not live"` suite need no network and no key (§1.4). The switch is deliberately **not**
"use Voyage if `VOYAGE_API_KEY` is set": this repo's `.env` carries the key, so that rule
would put the gated suite on the network the moment anyone ran it with
`set -a; source .env`. The consequence is that the droplet keeps using bag-of-words
retrieval unless the variable is set *and* the KB is reseeded with it. **Flipping the
variable without reseeding is worse than not flipping it** — query vectors and stored
vectors would be from different spaces, which produces plausible-looking nonsense rather
than an error. Track F owns compose/env forwarding; this is one row in `.env.example` and
both compose files, plus a reseed in W3-G3.

Why it is worth doing, measured on the 12 held-out queries in
`backend/tests/data/test_retrieval.py` plus 12 topic paraphrases and 7 off-domain probes:

| | rank-1, customer wording | rank-1, topic paraphrase | off-domain probes rejected |
|---|---|---|---|
| `HashingEmbedder` (default) | 10/12 | **5/12** (3 retrieve nothing) | 3 of 5 |
| `VoyageEmbedder` | **12/12** | **11/12** | **5 of 5** |

The topic-paraphrase column is the one that matters operationally: `agent.nodes.kb_answer`
searches with `state["topic"]`, the classifier's one-sentence paraphrase, **not** the
customer's words. `docs/BUILD-PLAN.md §11`'s risk that "Voyage makes retrieval worse" is
retired — it is better on every axis measured, and the gap is not close.

**(b) The Voyage account has no payment method,** so it is on the free tier: **3 requests
per minute, 10,000 tokens per minute** (the 429 body says so verbatim). A whole-KB batch is
~12k tokens — *over the per-minute ceiling in a single request* — so a naive reseed 429s
forever rather than slowly. `VoyageEmbedder` handles it (16-item batches, fixed 20s
backoff, 8 attempts), and a 44-chunk reseed takes ~2 minutes. Two things to weigh before
the live e2e run: **query-time** embedding also costs one request per `search_kb` call, so
3 RPM is a hard ceiling on agent throughput, and W4's 20–30-ticket scenario run would be
throttled by it. Adding a payment method lifts the limits within minutes and the free token
grant still applies.

### 10.4 W3-G2 measured findings — the deep check works, and it found a live blocker

**2026-08-17, W3-G2.** `scripts/verify_deploy.sh --deep` + `scripts/verify_core_loop.py`.

**(a) The check does what it was built to do.** Against the droplet, assertions 1–4 pass and
the deep check **fails**: `no new runs row for ticket cxforge-verify-W3G2 after 240.9s`,
exit 1. Against a stack assembled on this machine with a real Redis, a real `arq` worker,
real Anthropic calls and the real Postgres, it **passes** in 8.2s with a `runs` row whose
`replied_at - received_at` is **7.576s** — against the 22µs the pre-ADR-004 code produced.
The Redis hop `docs/STATE.md §1` names as "the largest remaining unknown" has now been
crossed by a real connection: the `cxforge:jobs` list held a pickled `run_ticket` job
carrying `ticket_id`, `comment_id` and `received_at`, exactly as §1.1 froze it.

**(b) Blocker — `ZENDESK_OAUTH_TOKEN` is dead again (401 `invalid_token`), so no deployment
can produce a `runs` row at all.** `ingest` calls `fetch_ticket` first; a 401 there fails the
run, releases the dedup row (ADR-003) and writes nothing. **This gates W3-G3 as much as
W4**: a redeploy with a connected loop and this token gives a stack that is healthy in
`docker compose ps`, green on `verify_deploy.sh` 4/4, and unable to answer a single ticket —
with arq booking every failed run as `success = True`. Re-auth is OA-4, two minutes, needs a
browser. **Do it before G3, not after.**

**(c) Consequence for G2's own verification, stated plainly.** With the trial unreachable,
the passing demonstration was obtained by stubbing **only** the Zendesk vendor — a local
HTTPS server reached through `ZENDESK_SUBDOMAIN`, with everything else (HTTP ingress, Redis,
the arq worker, `run_agent`, Anthropic, Postgres, the portal API) real and unmodified. So
what is proven is every hop except the live Zendesk API, which `backend/tests/helpdesk`'s
contract suite covers over mocked HTTP and `scripts/live_smoke.py` covers by hand. What is
**not** proven is a green `--deep` against a real deployment; that needs OA-4, then G3.

**(d) The deep check is opt-in, and a run without it now says so.** Every `PASS` prints a
`SCOPE:` line on the next line stating what it does not cover, because an unqualified PASS
from four liveness assertions is precisely what carried "the deploy works" for weeks
(`docs/STATE.md §6.2`).

### 10.5 W3-G3 measured findings — the droplet now runs the real loop, and two things gate it

> **Both gates cleared 2026-08-17 — see §10.6.** (b) the credential, and (c) the hostname. (g)'s
> "not proven" list is now proven end to end.

**2026-08-17, W3-G3.** The droplet at **`161.35.2.250`** (DigitalOcean id `592687747`, name
`cxforge`; the other two droplets on the account were not touched) was redeployed by rsync +
`scp .env` + `deploy/compose.sh up -d --build --wait`. Six services, all reporting healthy,
`--wait` exit 0. Everything below was read back from the running system.

**(a) The Redis hop is crossed on the real deployment.** `docs/STATE.md §1` called it "the
largest remaining unknown" and noted no code in this repo had ever opened a real Redis
connection. It has now. A correctly HMAC-signed synthetic webhook at
`http://161.35.2.250:8080/webhooks/zendesk` returned `202 {"status":"accepted","duplicate":false}`,
and the droplet's arq worker picked the job up in **0.11 s**:

```
0.11s → 2427dbf78d2b4377aa515dfad509a292:run_ticket({'ticket_id': '3', 'comment_id': 'cxforge-ve…
"message": "agent run started", "ticket_id": "3", "comment_id": "cxforge-verify-8e7540b6…"
```

`run_agent` then entered LangGraph and reached `ingest`. So ingress → `tickets_seen` →
real Redis → real arq worker → `run_agent` → `nodes.ingest` all work **on the droplet**.

**(b) `scripts/verify_deploy.sh --deep` still FAILS, and for a reason that is not the
deploy.** `ingest`'s `fetch_ticket` got **401 `invalid_token`** from Zendesk, ADR-003
released the dedup row, the ERROR was logged with a full traceback, and no `runs` row was
written — precisely the sequence §10.4(b) predicted. What is new is *why*, and it is worse
than "the token is dead": **the token is a JWT whose only claim is `exp`, and it lives about
25 minutes.** It answered 200 at 05:38, its `exp` was `2026-08-17T06:03:13Z`, the worker used
it at 06:05:36 and got 401, and the same token 401'd from the developer's laptop at 06:07 —
byte-identical (sha256 `9d39383bf53efa5e…`) in `.env`, on the droplet, and inside both
containers. Nothing revoked it; it expired. See `docs/OWNER-ACTIONS.md` OA-4, which now
carries the timeline, the consequence for ADR-015's 20–30-ticket run, and the measured
evidence that the client **would** honour a `refresh_token` grant that
`scripts/zendesk_oauth.py` never asks for.

**(c)** ~~**The public hostname is still 502, and it is one dashboard dropdown.**~~
**RESOLVED 2026-08-17 — and the diagnosis below was half right.** The scheme *was* wrong, but
the fix was not `HTTP → backend:8000`: the Service is now **`portal:80`**, so one hostname
serves the portal SPA, `/api/`, `/webhooks/` and `/health` through the portal container's
nginx. See §10.6. *Original finding, preserved:* `cloudflared`
runs with `readyConnections: 4`, but the ingress rule Cloudflare pushes down says
`"service":"https://backend:8000"` while uvicorn serves plain HTTP (`http://backend:8000/health`
→ 200; `https://` → `SSL routines::wrong version number`). `deploy/cloudflared/README.md`
and OA-3 step 3 both record the rule as Service type **HTTP**. Full evidence, the two
failed workarounds, and the fix are in OA-3's banner. **This blocks the Zendesk webhook**
(already re-pointed at `${PUBLIC_BASE_URL}/webhooks/zendesk`), not `verify_deploy.sh`.

**(d) Defect found and fixed in this package: the worker's healthcheck could never pass.**
A container inherits the image's `HEALTHCHECK` unless the service overrides it, and
`deploy/Dockerfile.backend`'s is an HTTP GET to `127.0.0.1:8000/health`. The `worker`
service overrode nothing, so it ran `arq`, served no HTTP, and sat at `Up (unhealthy)` with
FailingStreak 25 while consuming jobs normally — and `deploy/compose.sh up -d --build --wait`
exited **1** with "container othram-deploy-worker is unhealthy" on an otherwise successful
deploy. Both compose files now declare a probe for the health-check key arq writes into
Redis, assembled from `worker.settings.QUEUE_NAME` + `arq.constants.health_check_key_suffix`
so no literal is duplicated. `arq --check` was tried first and rejected on measurement: it
imports the whole agent stack and takes **16.5 s** on this 2-vCPU droplet versus **1.8 s**
for the key probe. Pinned by
`backend/tests/deploy/test_compose_topology.py::test_the_worker_overrides_the_images_http_healthcheck`,
which was confirmed red with the override removed and red again with an HTTP-8000 probe
substituted.

**(e) Everything a successful run needs, proved separately from inside the deployed
containers** — because the run itself never got past `ingest`:

| Hop | Read back from the droplet |
|---|---|
| Anthropic | `messages.create` → `model: claude-opus-5`, `stop_reason: end_turn`, text `deployed`, 17 in / 5 out |
| Langfuse | `GET /api/public/projects` with the deployed keys → **200**, `name: "cxforge"`, org `hank-personal`, host `https://us.cloud.langfuse.com` |
| Retrieval | `KB_EMBEDDER=hashing`, `HashingEmbedder`, `search_kb` → 3 hits, top `pipeline-stages-overview` @ 0.289 |
| Seeding | bootstrap logged *"NOT seeding — 30 cases and 44 kb chunks are already present"*; both counts unchanged after the redeploy |

**(f) Production still retrieves lexically, deliberately.** `KB_EMBEDDER=hashing` was pinned
explicitly in the droplet's `.env` rather than left to the compose default. Flipping to
Voyage needs the reseed in the same window (§10.3) and is a separate operation.

**(g)** ~~**Not proven, and it needs to be said plainly.**~~ **ALL OF IT IS NOW PROVEN —
2026-08-17, by a real customer comment on ticket 3. See §10.6.** Including the last sentence:
the missing `notification_webhook` trigger was the Wave 4 prerequisite this paragraph
identified, and it now exists. *Original finding, preserved:* Nothing downstream of `ingest` has run
on the droplet: no `classify`/`compose`/`verify`/`act`, **no `runs` row has ever existed
there** (the table is still empty), no draft, no Langfuse trace *from a droplet run*, and no
public reply posted by the deployed agent. The tunnel has carried no request to the origin.
And separately from OA-3: the Zendesk account has **zero active triggers with a
`notification_webhook` action**, so even with the tunnel fixed, a real customer comment
would not reach the droplet — only the synthetic POST `--deep` sends. That is a Wave 4
prerequisite nobody has written down yet.

**(h) Residue.** The pre-existing `tickets_seen` row from §10.4 was deleted
(`DELETE 1`, table now 0 rows). `--deep`'s own row was already gone — ADR-003 released it on
the failed run — so its cleanup reported `tickets_seen=0`. `cases`/`kb_chunks` are 30/44,
`runs`/`drafts` are 0/0. One `arq:result:*` key remains in Redis with arq's default TTL;
that is arq's own bookkeeping, not application state. Zendesk **ticket 3**
("W3-G3 deploy verification — case status question", requester `w.park@example.com`, tag
`cxforge-verify`) was created as the disposable `CXFORGE_VERIFY_TICKET_ID` and left in place
deliberately — the check is designed to reuse one ticket, and the retry after OA-4 needs it.

### 10.6 The loop ran end to end on the droplet, driven by a real Zendesk comment

**2026-08-17.** Everything here was read back from the live systems.

**(a) The run.** A real public comment from the requester on live Zendesk **ticket 3**:
Zendesk trigger fired (`WebhookEvent` in the audit) → Cloudflare tunnel → droplet ingress →
**real Redis** → arq worker dequeued in **0.03s** → `run_agent` → `fetch_ticket` 200,
`fetch_conversation` 200, `fetch_requester_history` 200 (**ADR-009 working in production**) →
2 × Anthropic `claude-opus-5` → 3 × PUT to Zendesk → `agent run completed, duration_s: 11.477`.
Route `case_status`, confidence **0.98**, outcome **`auto_sent`**. The reply is **publicly
posted** and carries ADR-020's disclaimer verbatim ("an estimated timeline, and subject to
change"). A Langfuse trace **from the droplet run** — `81cdbd81bdbc474eafac148ae997a51b` —
landed in project `cxforge`. **This is SPEC success criterion 1 against the deployed system.**

**(b) Metrics are real.** `/api/metrics` through the tunnel: `sample_count: 3`,
`latency_p50_s: 15.0`, `latency_p95_s: 15.3` — against the vacuous `0.0` that
`docs/STATE.md §4.1` warned would read as a passing p95. Inside R8's 5-minute bar **with
evidence behind it**, on 3 data points rather than ADR-015's 20–30.

**(c) One hostname serves everything.** Service repointed to **`portal:80`**; the portal
container's nginx proxies `/api/`, `/webhooks/` and `/health` to the backend and serves the SPA
at `/`. `/` 200, `/health` 200, `/api/*` 401 without a token, `/webhooks/zendesk` 401 unsigned.
No droplet port exposed. **The Zendesk webhook URL did not change** — verified *before*
recommending the change, not after: a payload signed with the server's own `compute_signature`
returned **202 through nginx** and **202 direct**.

**(d) The Zendesk credential renews itself, and the mechanism had been misdiagnosed three
times.** The access token is a JWT with **exactly one claim (`exp`)** and a lifetime of
**exactly 1800s**; `expires_in` is not a lever (86400 / 172800 / 604800 all returned 1800).
**A refresh token was being issued all along:** `scope` is `read write` with no
`offline_access`, and Zendesk returns a **30-day** `refresh_token` regardless. The old
`scripts/zendesk_oauth.py` did `.get("access_token")` and discarded the rest — and the grant
response is the **only** place the refresh token is readable in full — so every one was thrown
away unrecoverably. Rotation is now **proven**: `--refresh` run twice, access token rotated
both times, refresh token rotated both times (Zendesk invalidates the spent one), each new
access token returned 200, **no browser**. Standing procedure is in OA-4. API tokens were never
an alternative: `docs/SPEC.md:147` forbids them **and** this account (admin user created
2026-08-14) falls after Zendesk's 2026-07-28 cutoff that blocks creating them at all.

**(e) Three production defects the run exposed**, all fixed and verified live, recorded where
the stale claim was: the `ai-processed` loop-guard tag was **inert**
(`docs/STATE.md §5.1` — `additional_tags` is not a field of the ticket-update schema and
Zendesk discards unknown keys with a 200; and on the tags sub-resource `PUT` is additive while
`POST` is destructive, the reverse of the UI labels); `ZENDESK_AI_USER_ID` named a user the
token never acts as, so **both loop guards were down simultaneously** and only an unrelated
`{{ticket.latest_comment_id}}`-renders-empty bug stopped an infinite reply loop
(`docs/STATE.md §6.15`); and the account had **zero triggers with a `notification_webhook`
action**, so nothing ever fired the webhook — trigger `54508374798747` now exists and was
verified by read-back (`docs/STATE.md §6.16`, the Wave 4 prerequisite §10.5(g) identified).

**(f) CI is green for the first time.** It had failed **30 of 30 runs** — every run ever —
on a `backend/tests/hooks` test that faked "no interpreter" with `PATH=/bin`: true on macOS,
false on Ubuntu where `/bin` is a usrmerge symlink. **The guard was correct; the test was wrong
about Linux.** That suite is retired (ADR-019 amendment) and run **32003095488** shows
`Lint ✓ Type-check ✓ Portal contract ✓ Test ✓` with **511 passed**.

### 10.7 Open questions from the live run — not decided, do not resolve in passing

Each of these is the owner's call. None is a defect; none has been decided.

**(a) Refresh-token rotation does not survive a container restart.** Zendesk invalidates a
spent refresh token, so a container's copy in its environment dies the first time that
container refreshes. In-process renewal is durable for a process's lifetime and **not** across
`docker compose restart`. Fixing it properly needs a store the containers share (the database).
That is a **scope decision, not a bug fix**.

**(b) Every customer-visible reply is authored by "Hank Holcomb", admin.** Switching to the
dedicated "Othram AI Agent" identity (`54404962250395`) is **demo optics, not correctness**, and
needs an OAuth consent as that user — which is `role: agent` and may lose permissions the admin
token has (group assignment, some search scopes). If it is done, do it via a `scripts/live_smoke.py`
run, not as a discovery during filming.

**(c) Two CI guards cannot do their job.** `ruff check .` in CI **silently skips 19 files** —
`extend-exclude = ["portal", ...]` is gitignore-style, so `backend/src/portal` and
`backend/tests/portal` have never been linted in CI. `.claude/rules/build-protocol.md` rule 2
already requires the explicit invocation **locally**; CI does not do it. They are clean today, so
adding it would pass — widening the CI gate is the owner's call. Separately, the **collected-count
floor is 200 against 511 actual**, so it would not notice losing 60% of the suite.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| ~~The Zendesk OAuth **client** was deleted, not just the token expired.~~ **RETIRED 2026-08-16 — measured, not assumed.** | The OAuth client is **alive and authenticating**. Probe: POST `/oauth/tokens` with the real `client_id`/`client_secret` and a deliberately bogus `code` → `invalid_grant` (400). Control with a wrong `client_id` → `invalid_client` (401), so the discriminator is real. Only the access token is dead. OA-4 is the 2-minute re-auth; no OAuth-app rebuild, and the runbook's OAuth-app section stands. |
| Trial lapses ~2026-08-27 mid-recording. | Wave 4 is scheduled before it; `zendesk-runbook.md` gains a "trial expired" branch in J2. |
| ~~Voyage reseed makes retrieval **worse** than the lexical embedder for these queries.~~ **RETIRED 2026-08-16 — measured, not assumed (§10.3a).** | Voyage is better on every axis measured: rank-1 12/12 vs 10/12 on the held-out queries, **11/12 vs 5/12** on the topic paraphrases `kb_answer` actually searches with, (3 of hashing's 12 retrieve **nothing**; Voyage retrieves on all 12), and it rejects 5/5 off-domain probes vs 3/5. The margin is not close: hashing's in-domain score band (0.1068–0.3762) **overlaps** its off-domain band (0.0542–0.2238) — "Who won the World Cup final in 2022?" outscores the correct chunk for 5 of the 12 queries, so no single relevance floor can separate signal from noise on it at all. Voyage's bands are cleanly separated (0.2929–0.6336 in-domain vs 0.1110–0.2644 off-domain), which is what makes B2's `KB_MIN_SCORE` calibratable. `HashingEmbedder` remains the offline default and the one-line revert. The live risk is now the opposite one: production stays lexical until `KB_EMBEDDER=voyage` is set **and** the KB is reseeded — see §10.3a. |
| Live regeneration moves the published 1.000 eval numbers. | Accepted deliberately (ADR-007). The new numbers ship. |
| The real model routes a canonical scenario differently than the fakes assume — nothing measures this today. | E3 exists precisely to find this before camera time is spent. Run it before scheduling a recording. |
| Two concurrent pytest processes corrupt each other's DB schema. | Never run the suite while a subagent runs it; `test_concurrency.py` fails if a third process touches the same database. |
