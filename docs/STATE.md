# cxforge — verified state, 2026-08-16 · updated 2026-08-17

**This file supersedes `docs/HANDOFF.md` and the entire `.claude/NEEDS_HUMAN.md` log as
the entry point.** Both remain on disk as historical record; neither is current. Where
they disagree with this file, they are stale.

Everything below was established by direct code read, a command that was run, or a live
HTTP call — not by reading a docstring or another document. Claims sourced from a doc
are labelled as such. A 50-agent audit produced these findings and 40 adversarial
verifications checked them; 11 verifications overturned or materially corrected the
finding they examined, and those corrections are folded in here.

---

## 1. The one-sentence version

> ### ✅ THE SYSTEM ANSWERED A REAL ZENDESK TICKET, END TO END, ON THE DEPLOYED DROPLET — 2026-08-17
>
> **This is SPEC success criterion 1 demonstrated against the deployed system.** No synthetic
> POST, no stub: a real public comment from the requester on live Zendesk **ticket 3** drove
> the whole chain.
>
> ```
> Zendesk trigger fires (WebhookEvent in the audit) → Cloudflare tunnel → droplet ingress
>   → real Redis → arq worker dequeues in 0.03s → run_agent
>   → fetch_ticket 200 · fetch_conversation 200 · fetch_requester_history 200 (ADR-009 in production)
>   → 2 × Anthropic claude-opus-5 → 3 × PUT to Zendesk
>   → "agent run completed, duration_s: 11.477"
> ```
>
> The reply is **publicly posted on the ticket** and carries ADR-020's disclaimer verbatim
> ("an estimated timeline, and subject to change"). Route `case_status`, confidence **0.98**,
> outcome **`auto_sent`**. A Langfuse trace **from the droplet run** —
> `81cdbd81bdbc474eafac148ae997a51b` — landed in project `cxforge`.
>
> `/api/metrics` through the tunnel now reports `sample_count: 3`, `latency_p50_s: 15.0`,
> `latency_p95_s: 15.3` — **real data where it previously reported a vacuous `0.0`**, and
> comfortably inside R8's 5-minute bar with evidence behind it.
>
> Five things had to be fixed to get here, each recorded where the stale claim was: **§3.2**
> (the tunnel served nothing), **§5.1** (the loop-guard tag was inert in production), **§6.15**
> (the AI user id was wrong — **both loop guards were down at once**), **§6.16** (no Zendesk
> trigger existed, so nothing ever fired the webhook) and `docs/OWNER-ACTIONS.md` **OA-4** (the
> credential renews itself now, with no browser). Measured detail: `docs/BUILD-PLAN.md §10.6`.

> ### ⚠️ SUPERSEDED — 2026-08-16, Waves 1 and 2
>
> **§1 and §2 below describe the state this audit found. They are no longer true of the
> repo.** Wave 1 Track A connected the loop; Wave 2 built retrieval quality and
> observability on top of it.
>
> **Wave 1 — committed**, seven commits, `b9babe7`..`972c13b`. The ingress handler enqueues
> a `TicketJob` and an arq worker consumes it and calls `run_agent`. Verified
> independently: a signed webhook drives the real FastAPI app and produces one enqueued job
> carrying a handler-stamped `received_at`; the worker passes that stamp through to
> `runs.received_at`; and the old latency defect was measured at **22µs** across a 300ms
> ingest delay, against **≥700ms** now. Wave 1 also landed the promptfoo suite and the
> first live route-accuracy measurement (Track E), the portal's stylesheet and outcome
> rendering (Track D), and the redis/worker/cloudflared services with `.env` loading and an
> env-forwarding audit test (Track F).
>
> **Wave 2 — landing with this commit.** Track B: `VoyageEmbedder`, a `KB_MIN_SCORE`
> relevance floor, the ADR-011 permission rewording, and customer history through
> `HelpdeskPort` (ADR-009). Track C: real Langfuse instrumentation, structured JSON
> logging, and honest metrics. The full gate is green at **810 passed, 2 deselected**
> (Wave 1 ended at 716).
>
> **Precise about what is and is not proven:**
>
> - ~~**No code in this repo has ever opened a real Redis connection.**~~ **OBSOLETE —
>   crossed 2026-08-17.** *Original finding, preserved:* `ArqJobQueue` has only ever met a
>   stub. The Redis hop is the largest remaining unknown and no unit test can close it; it
>   needs `docker compose up`, a signed webhook, and a `runs` row read back (Wave 3 **G2**).
>   **Now:** crossed locally (`docs/BUILD-PLAN.md §10.4a`), then on the droplet with a signed
>   synthetic webhook (§10.5a), and finally by a **real Zendesk comment** — the droplet's arq
>   worker dequeued that job in **0.03s**. See the banner above.
> - **Production still retrieves lexically.** `KB_EMBEDDER` defaults to `hashing`, and
>   switching to Voyage requires the env change **and** a KB reseed (§10.3 of
>   `docs/BUILD-PLAN.md`) — flipping without reseeding returns plausible nonsense, not an
>   error.
> - **Langfuse is the one edge verified against the vendor**, not a fake: trace
>   `422bccf6fc854007b2cefb47ff80ce56` in project `cxforge`, 8 spans, `/trace/<id>` → 307 →
>   200.
>
> ~~Everything downstream of the loop in §1 and §2 — `runs` empty, the feed reading
> "No runs yet.", metrics at 0.0 — remains **true of the deployed droplet**, which still
> runs the pre-Wave-1 image.~~ **OBSOLETE — 2026-08-17.** The droplet runs the current
> image, `runs` has rows written by real tickets, and `/api/metrics` reports
> `sample_count: 3` with p50 `15.0s` / p95 `15.3s`. See the banner above.

Every component works and is tested; **nothing assembles them**. The Zendesk webhook
accepts events and returns 202 without ever starting an agent run, so no agent run has
ever executed outside a test process, `runs` is empty everywhere, the portal feed reads
"No runs yet.", and every metric is 0.0.

---

## 2. The core loop is severed — verified four independent ways

| Evidence | Finding |
|---|---|
| `backend/src/ingress/__init__.py:89` | Handler's last statement is `return {"status": "accepted", "duplicate": not is_new}`. The file imports nothing from `agent`. |
| repo-wide grep for `run_agent` | 20 references. 1 definition (`agent/graph.py:89`), 2 re-exports, 3 docstrings in `backend/src`, 5 doc mentions, **15 invocations — all under `backend/tests/`**. Zero in `backend/src/`, `scripts/`, `deploy/`, `portal/`, `evals/`. |
| live droplet | `GET /api/feed` → `{"runs":[]}`; `GET /api/metrics` → all zeros. |
| dispatch machinery search | No `BackgroundTasks`, no `asyncio.create_task`, no scheduler, no queue, no worker service. `deploy/docker-compose.yml` has exactly `db`, `backend`, `portal`. |

**It is nobody's fault and nobody's job.** Three source files point at each other in a
circle: `ingress/__init__.py:22-23` says starting the run is "T-5's job … deliberately
never invoked from here"; `portal/deps.py:17-20` says it is "T-10's scenario runner's
job"; T-5's scope is `backend/src/agent/**` (cannot reach ingress) and T-10's is
`backend/tests/live/**` + `scripts/scenario_runner.py` (cannot reach `backend/src`).
`docs/DESIGN.md` (§ *Components*) *does* name "enqueue" as an ingress responsibility — so
this is an omission in **T-4's acceptance criteria**, not a gap in the design.

### T-10's deliverables were never written

`scripts/scenario_runner.py` **does not exist**. `backend/tests/live/` **does not
exist**. T-10 is not a blocked ticket; it is an unstarted one whose acceptance criterion
3 ("latency report, webhook to reply") is unsatisfiable until the loop is connected.

---

## 3. Live checks run 2026-08-16

| Check | Result |
|---|---|
| Zendesk token | **HTTP 401**, body `{"error":"invalid_token","error_description":"The access token provided is expired, revoked, malformed or invalid for other reasons."}` |
| Zendesk trial | **Alive.** `hank-43016.zendesk.com` resolves; the unauthenticated `/api/v2/users/me.json` answers as "Anonymous user". Only the token is dead. |
| Zendesk OAuth **client** | **Alive and authenticating** — measured, not assumed. POST `/oauth/tokens` with the real `client_id`/`client_secret` and a bogus `code` returns **`invalid_grant` (400)**; the control with a wrong `client_id` returns **`invalid_client` (401)**. So the client exists and only the access token is dead. This **retires the top risk in `BUILD-PLAN.md §11`**: OA-4 is the 2-minute re-auth, not an OAuth-app rebuild. (Incidental: the client's identifier is `jarvis` — harmless, since a Zendesk OAuth client is account-wide, not project-scoped.) |
| Droplet `:8080/health` | **200** |
| Droplet TLS | **None.** 443 and 8443 refuse connections; port 80 times out (408). Zendesk requires an HTTPS webhook endpoint — **it cannot reach the droplet as deployed.** |
| Langfuse keys | Authenticate (`auth_check() → True`), resolve to project **"jarvis"** / org **"hank-personal"** (confirmed live via `GET /api/public/projects`). `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` hold the **byte-identical 42-char `sk-lf-…` value** — no public key is configured at all. **Two independent defects, one shared fix — see §3.1.** |
| Stray `* 2.py` files | Both exist, both differ from their originals, and pytest **collects 7 tests** from `backend/tests/hooks/test_close_unattributed_claim_gap 2.py`. They also make the tree dirty, which blocks the harness's claim-time check. *(Path as measured that day. Cleared by W0.1, and `backend/tests/hooks/` itself no longer exists — ADR-019's 2026-08-17 amendment retired the whole directory to `.claude/harness-archive/hooks-tests/`.)* |
| **Full suite** | **`2 failed, 707 passed` in 4m46s** — measured 2026-08-16, not inherited from a doc. **The suite is not green.** Both failures are caused by the stray duplicate; see §4.4. |
| **Full suite, after W0.1** | **`702 passed, 0 failed`, 1 deselected in 4m34s** — re-measured 2026-08-16 after moving both stray `* 2.py` files out of the tree. `ruff check .`, `ruff check backend/src/portal backend/tests/portal`, and `mypy backend` (130 files) all clean. **This exactly matches §4.3's arithmetic prediction (709 − 7 = 702), so the duplicate was the whole cause and nothing else is wrong.** This is the baseline every work package must meet or beat. |

---

## 3.1 Langfuse: two independent defects, one shared fix

The two problems in the row above are usually conflated. They are not the same defect, and
only one of them actually bites.

**Defect A — wrong project (this is the one that matters).** In Langfuse there is no
project setting anywhere in config: **the key pair *is* the project pointer.** A pair is
minted inside one project, and traces authenticated with it land in that project. So "a
past agent reused the existing `jarvis` project" and "`jarvis`'s keys are in `.env`" are
one fact, not two. Demo traces would land amid unrelated personal work.

**Defect B — no public key.** Both env vars hold the same secret. A plain paste error,
independent of A.

**Why B survived undetected** — the same stub-blindness pattern as the rest of this repo.
Measured against `us.cloud.langfuse.com` on 2026-08-16:

| Probe | Result |
|---|---|
| garbage public key `pk-lf-deadbeef` + real secret | **200** |
| real secret in *both* slots (today's `.env`) | **200** |
| real public slot + wrong secret | 401 |
| empty public slot + real secret | 401 |

Langfuse Cloud authenticates on the **secret alone**; the username half need only be
non-empty. **`auth_check() → True` is structurally incapable of detecting defect B**, which
is precisely why it went unnoticed. Do not treat that call as evidence the pair is correct.

**Severity correction.** A POST to the real ingestion endpoint with today's doubled-secret
pair returns **207**, so traces would be *accepted* right now — into `jarvis`. Defect B is
therefore close to harmless over raw REST; defect A is the real problem. The caveat is that
W2-C1 uses the Langfuse **SDK**, not raw REST, and the SDK does want a genuine `pk-lf-…`.

**The fix is one action, not two.** Creating the `cxforge` project is what *mints* the
correct pair — there is no way to fix A without fixing B. See `docs/OWNER-ACTIONS.md` OA-2.

---

## 3.2 The Cloudflare tunnel is live, and one hostname serves everything — 2026-08-17

`https://cxforge.hankholcomb.com`, measured from the public internet. **No droplet port is
exposed.**

| Path | Result |
|---|---|
| `/` | **200** — the portal UI |
| `/health` | **200** |
| `/api/*` | **401** without a token |
| `/webhooks/zendesk` | **401** unsigned |

The owner repointed the tunnel's Service to **`portal:80`** — not `backend:8000`, which is
what OA-3 and `deploy/cloudflared/README.md` originally specified. The portal container's
nginx proxies `/api/`, `/webhooks/` and `/health` to the backend and serves the SPA at `/`.

**The Zendesk webhook URL did not change**, and that was verified *before* the change was
recommended rather than after: a payload signed with the server's own `compute_signature`
returned **202 through nginx** and **202 direct**, so a valid HMAC signature survives the
proxy. Corrected history — including the prediction that got this wrong — is in
`docs/OWNER-ACTIONS.md` OA-3.

---

## 4. Four defects not previously recorded anywhere

### 4.1 R8/R13 latency measures the wrong interval

`received_at` is minted at `backend/src/agent/nodes.py:591` — **inside `act`, the last
node of the graph**. So `replied_at - received_at` times only the HelpdeskPort calls,
excluding ingest, classify, retrieval, compose, verify and decide — i.e. every model
call. Webhook-receipt time is persisted nowhere: `tickets_seen`
(`backend/src/data/schema.py:104-108`) has no timestamp column and the ingress handler
records no time.

`docs/DESIGN.md` (§ *Latency*) pins latency as "webhook receipt → public reply posted", and
`backend/src/portal/service.py:307-311` quotes that definition verbatim in a docstring.
**Both are false about the code beneath them.** Connecting the core loop does not fix
this; it must be fixed deliberately.

~~Today `/api/metrics` reports p50 = p95 = 0.0 because there are no runs — which would
make success criterion 6 ("p95 < 5 min") *vacuously true* if read off the panel.~~
**CORRECTED 2026-08-17:** the interval was fixed by ADR-004 (receipt stamped in the ingress
handler and threaded through), W2-C3 made an empty sample report honestly, and the deployed
panel now reads `sample_count: 3`, p50 `15.0s`, p95 `15.3s` from real tickets. Criterion 6 is
no longer vacuous — it is 3 data points against ADR-015's 20–30.

### 4.2 R15's headline number rests on a smaller base than it appears

`docs/eval-report/metrics.json` reports P = R = F1 = 1.000 and hard-trigger recall
1.000. That recall is computed over **10 of the 16** labeled hard-trigger tickets:
`evals/report.py:486-517` marks 2 `out_of_procedure` and 4 of 5 `low_confidence`
tickets structurally unmeasurable (they are decided by agent graph nodes the eval never
drives) and `main()` excludes them, giving `measured_sample_size: 45` of 51 labels.

If the 6 excluded tickets counted as misses, hard-trigger recall would be **0.625** —
below R15's 0.95 bar. The artifact is honest about the exclusions (it lists them by id
with reasons); what is missing is that framing appearing next to the 1.000.

Also missing from `metrics.json`: any **model identifier** (nothing distinguishes a
GPT-era run from a `claude-opus-5` one) and the **threshold sweep** (so its
"recommend 0.00" cannot be checked against the shipped 0.50).

### 4.3 ~~The suite is red right now~~ — RESOLVED by W0.1; one stray file was the whole cause

Measured 2026-08-16: **`2 failed, 707 passed, 1 deselected` in 4m46s.** The transcript below
is verbatim and is left as measured; note that `backend/tests/hooks/` no longer exists —
ADR-019's 2026-08-17 amendment retired the whole directory to
`.claude/harness-archive/hooks-tests/`, and `test_skip_db_tests_relocation.py`'s *sample*
directory (never its assertion) moved to `backend/tests/contract` with it.

```
FAILED backend/tests/hooks/test_close_unattributed_claim_gap 2.py::
       test_close_silently_mints_a_receipt_when_session_field_disagrees_with_filename
FAILED backend/tests/test_skip_db_tests_relocation.py::
       test_skip_db_tests_leaves_an_unrelated_directory_unaffected
```

Both trace to the same root cause. The first is the untracked macOS duplicate itself — a
pre-fix revision asserting the **opposite** of current behaviour (it expects a minted
receipt where `cmd_close` now refuses). The second is collateral:
`test_skip_db_tests_relocation.py:90` spawns a **child pytest run** and asserts its
`returncode == 0`; that child collects the same duplicate and reports
`1 failed, 335 passed`, so the parent assertion fails too.

**Arithmetic prediction, worth checking rather than trusting:** 707 + 2 = 709 tests ran,
of which the duplicate contributes 7. Removing it should give **702 passed, 0 failed** —
which matches the 702 figure recorded historically. If clearing the stray files does not
produce exactly that, something else is also wrong and needs investigating before Wave 1
starts.

### 4.4 `full_verify` has provably never fired

`docs/tickets.json` now carries `"full_verify": "uv run pytest -m \"not live\" -q"`
(added in `7c61391`), but `git merge-base --is-ancestor 7c61391 <close-commit>` is false
for all three most recent closes. The next close would run it for the first time in the
repository's life — against a tree containing the stray duplicate test module.

---

## 5. What is genuinely strong

These are load-bearing and should not be disturbed:

- **`ZendeskAdapter`** — all 7 `HelpdeskPort` operations over 3 real endpoints; ~~every
  write funnels through one `_update_ticket` that unconditionally folds in the
  `ai-processed` loop-guard tag~~ (**stale since 2026-08-17:** `_update_ticket` now *refuses*
  tag fields and the loop-guard tag is a separate additive write to the tags sub-resource —
  §5.1); 429 handling honours `Retry-After`; tenacity backoff on
  5xx; typed `HelpdeskAPIError` otherwise. The contract suite drives it over real
  `httpx` through a respx Zendesk simulator, so its green is evidence of real request
  construction. *Caveat:* ~~the additive-tag assumption is unverified against the real
  API~~ — **VERIFIED, AND IT WAS FALSE. This was a live production defect; fixed and
  verified live 2026-08-17 — see §5.1.** The tag is still never removed, so a ticket can be
  processed exactly once.
- **Grounding (R9)** — case facts are pure template-fill from a `data.Case` tool result
  (`agent/templates.py`, no LLM in the module); free generation happens only on
  `route == "kb"` (`nodes.py:357`), backstopped by a pure-Python, judge-independent
  `grounding_guard`. *Caveat:* the guard misses fabricated stage/ETA claims lacking a
  personalising cue word.
- **Escalation engine** — implements DESIGN's "any hard rule OR (classifier AND
  confidence ≥ threshold)" with correct short-circuit ordering.
- **Portal** — all 7 DESIGN-pinned endpoints implemented and token-gated, a real
  edit-then-approve queue, 44 backend + 5 vitest tests, all green.
- **Deploy stack** — compose, both Dockerfiles, nginx proxy, entrypoint bootstrap and
  the T-20 migration ledger are coherent; every env var the backend reads is forwarded.
- **Docs** — seven substantial technical docs. `docs/demo-script.md` is the most current
  file in the repo.
- **Suite** — 707 passing / 2 failing as measured today; both failures are the stray
  duplicate (§4.3), and clearing it should restore 702 green. ruff and mypy were clean at
  the last recorded run — re-verify as part of W0.1.

---

## 5.1 The loop-guard tag was inert in production — measured 2026-08-17

§5 above recorded the additive-tag assumption as unverified. It has now been verified against
the real API and **it was wrong**: after a complete, successful production run, the ticket
carried **no `ai-processed` tag at all**, so the trigger's nullifier condition was inert.

**Root cause, established with a control rather than inferred:** `additional_tags` **is not a
field of the single-ticket update schema, and Zendesk silently discards unknown keys with a
200.** A `banana_tags` probe behaved identically. All three of the worker's successful PUTs
therefore proved nothing.

**Second finding, counter-intuitive and a trap** — on the tags sub-resource the verbs are the
reverse of Zendesk's own "Add Tags" / "Set Tags" UI labels:

```
PUT  /tickets/{id}/tags.json   →  ADDITIVE
POST /tickets/{id}/tags.json   →  DESTRUCTIVE   (a POST probe wiped existing tags off the live ticket)
```

**Fixed and verified live:** after a real adapter write, an independent GET read the ticket
back as `['ai-processed', 'cxforge-live-verify', 'cxforge-verify']`.

**The respx simulator had been lying twice**, which is why the suite could not see either
defect: it implemented `additional_tags` as additive, and it hardcoded comment `author_id` to
the AI user id — the second of which made **§6.15 structurally unobservable**. Fixing the fake
turned **six pre-existing tests into real detectors** that fail against the old adapter.

---

## 6. Known-weak, in priority order

1. ~~**Nothing has ever run end-to-end.**~~ **FIXED — 2026-08-17. A real Zendesk ticket ran
   to a publicly posted reply on the droplet; see the §1 banner.** *Original finding,
   preserved:* All 78 graph/grounding/escalation tests drive a `FakeLLMClient`, and every
   canonical-scenario test **hands the route in** via a canned `Classification`.
   Route-classification accuracy — the thing R2–R5 all hinge on — is measured by nothing.
   **Also fixed:** W1-E3 measured it against the live model for the first time — 1.000
   (30/30) on the four branch routes (`docs/BUILD-PLAN.md §10.2`). The unit tests still hand
   the route in; that is now a gap in the *suite*, not a gap in the evidence.
2. ~~**Every deploy check is a liveness check.**~~ **PARTLY FIXED by W3-G2, 2026-08-17 —
   read the nuance.** *Original finding, preserved:* Nothing in `verify_deploy.sh`,
   `backend/tests/deploy/**`, or CI makes a model call, writes a row, or touches the
   agent path. That is precisely why the stack passed 4/4 for weeks with **no
   `ANTHROPIC_API_KEY` at all**, and passes 4/4 today with a dead core loop.
   **Now:** `bash scripts/verify_deploy.sh --deep` POSTs a correctly HMAC-signed
   synthetic webhook at the real endpoint and waits for a **new** `runs` row, read back
   through the deployed portal API — ingress → real Redis → real `arq` worker →
   `run_agent`. Demonstrated to fail against `161.35.2.250` (assertions 1–4 green, deep
   check `no new runs row … after 240.9s`, exit 1) and to pass in 8.2s against a stack
   with a real Redis and a real worker, with `replied_at - received_at = 7.576s`. It
   rules out the stale-row false pass with a baseline snapshot of run ids — verified
   against a live stack by killing the worker while a matching `runs` row sat in the
   table; the check still failed. **The nuance, three parts:** (i) it is **opt-in**, so a
   bare run is still liveness-only — which every `PASS` line now says out loud on the
   next line; (ii) **CI still runs none of it**, by design (SPEC §Constraints); and
   (iii) it has **never passed against a real deployment**, because `ZENDESK_OAUTH_TOKEN`
   is 401 again (OA-4) and `ingest`'s `fetch_ticket` is the first thing every run does —
   see `docs/BUILD-PLAN.md §10.4`.
   **Corrected 2026-08-17:** (iii)'s *blocker* is gone — the credential renews itself
   (OA-4) and a real ticket has since run to completion on the droplet, `fetch_ticket` 200
   included. Whether `--deep` **itself** has been re-run green against the deployment is
   **not measured**.
3. ~~**Langfuse is installed and imported nowhere.**~~ **FIXED by W2-C1 (ADR-006),
   2026-08-16.** *Original finding, preserved:* Zero `import langfuse` repo-wide.
   `nodes.py:592` mints `trace_id = uuid.uuid4().hex` and reports it to no one; the
   portal builds `{LANGFUSE_HOST}/trace/{that_uuid}`, which cannot resolve. *Currently
   unreachable* — the feed short-circuits to "No runs yet." with zero rows — so it is a
   latent defect, not a live one. **Now:** `agent.llm.emit_trace` is the single import
   site and `agent.nodes.act` reports the id it mints, so the portal link resolves.
   Verified against the vendor, not a fake: trace `422bccf6fc854007b2cefb47ff80ce56` in
   project `cxforge`, 8 spans, the `case_status` tool result (`case_id: MFG-2025-0734`)
   feeding the `compose` span whose draft quotes it, and `/trace/<id>` → **307 → 200**.
   Absent keys degrade to a no-op that never imports the package.
4. ~~**Retrieval has no relevance floor.**~~ **FIXED by W2-B2 (ADR-010), 2026-08-16.**
   *Original finding, preserved:* `search_kb` applies no score cutoff, so it
   always returns chunks — which makes R6's "empty retrieval" hard trigger **literally
   unreachable**. An escalation path the docs describe can never fire. **Now:**
   `KB_MIN_SCORE` is a per-embedder floor (Voyage `0.25`, hashing `0.09`) applied in
   `data.retrieval.search_kb`, and
   `backend/tests/grounding/test_empty_retrieval_escalation.py` demonstrates the trigger
   firing for the first time.
5. ~~**The "vector DB" is lexical.**~~ **PARTLY FIXED by W2-B1 (ADR-008), 2026-08-16 —
   read the nuance.** *Original finding, preserved:* `HashingEmbedder` is bag-of-words
   hashing. It works for in-domain queries (0.27–0.42 similarity on correct hits) but
   SPEC's constraints imply semantic embeddings. **Now:** `VoyageEmbedder`
   (`voyage-4-lite` @ `output_dimension=1024`) exists and is measurably better — rank-1
   **12/12 vs 10/12** on customer wording and **11/12 vs 5/12** on the topic paraphrase
   `kb_answer` actually searches with, and hashing's in-domain score band *overlaps* its
   off-domain band while Voyage's are cleanly separated (`docs/BUILD-PLAN.md §10.3`).
   **But production is still lexical:** `KB_EMBEDDER` defaults to `hashing`, and the
   switch takes the env change **and** a KB reseed in the same window, or retrieval
   returns plausible nonsense from mismatched vector spaces.
6. ~~**The permission route lies mildly.**~~ **FIXED by W2-B3 (ADR-011), 2026-08-16.**
   *Original finding, preserved:* The reply says the request "has now been
   processed for your case"; the codebase performs no such action anywhere. **Now:**
   `agent/templates.py` claims the **approval** — which is genuinely what the `permission`
   node decides, grounded in the KB's always-grant list — and says plainly that applying
   the change itself is someone else's step, so a customer whose request is never actioned
   knows to chase it.
7. ~~**Customer-history access**~~ **FIXED by W2-B4 (ADR-009), 2026-08-16.** *Original
   finding, preserved:* the one PRD line item that is in neither the code nor
   SPEC's non-goals. No `HelpdeskPort` method exists. **Now:** `fetch_requester_history`
   is on the port and implemented by **both** adapters, the contract suite covers it
   (R14 preserved), and the prior tickets reach the classifier's user message —
   subjects, status, age and tags only, never prior bodies.
8. ~~**Portal ships zero CSS**~~ **FIXED by W1-D, 2026-08-16, commit `e61fc78`.**
   *Original finding, preserved:* no `.css` file, no `className`, no `style` attribute,
   no tailwind/postcss, no CSS asset in the built bundle. **Now:** one stylesheet,
   `portal/src/styles.css`, and the build emits a CSS asset
   (`dist/assets/index-BuI7MQ4p.css`, 6.40 kB).
9. ~~**Portal renders no `run.outcome`**~~ **FIXED by W1-D, 2026-08-16, commit `e61fc78`.**
   *Original finding, preserved:* escalated and off_topic runs display as
   "auto-sent". `App.tsx` also matches `draft_id === null` when nothing is selected.
   **Now:** the feed renders the outcome it receives.
10. ~~**Logging is essentially absent**~~ **FIXED by W2-C2, 2026-08-16.** *Original
    finding, preserved:* two `logger.warning` calls in one module, no
    logging configuration anywhere, and zero mentions of logging in `docs/deploy.md` or
    `README.md`. **Now:** `backend/src/logging_setup.py` configures JSON-lines logging
    for both long-running processes (`backend/src/main.py`, `backend/src/worker/main.py`),
    gated off inside pytest so it cannot fight `caplog`.
11. ~~**`promptfoo` and `DeepEval` are named in SPEC and DESIGN and do nothing.**~~
    **FIXED by W1-E (ADR-013), 2026-08-16, commit `abcfd57`.** *Original finding,
    preserved:* `promptfooconfig.yaml` is still the T-1 scaffold with a placeholder
    prompt and `tests: []`; promptfoo is not installed; DeepEval is a dependency with
    zero imports. **Now:** `promptfooconfig.yaml` drives the shipped `agent.nodes.classify`
    through `evals/promptfoo/provider.py`, and DeepEval is gone from `pyproject.toml`.
12. **`README.md` is the worst-calibrated file in the repo** and the first thing a grader
    reads. It claims a quickstart "verified from a clean clone" containing a command that
    fails, says "No droplet exists" (it returns 200), says the Zendesk credentials are
    empty (all four are set), says "222 tests" (702), labels the eval report "Draft" (it
    is APPROVED/FINAL), omits `docs/escalation.md` from its own index, never mentions
    `VITE_PORTAL_TOKEN`, and never mentions the core-loop gap.
13. ~~**Provider drift in the plan docs.**~~ **FIXED by W0.3, 2026-08-16.** `docs/SPEC.md`
    and `docs/DESIGN.md` now name Anthropic for generation and Voyage for embeddings, with
    the pivot recorded rather than erased (ADR-014, ADR-008). The code was always entirely
    Anthropic (`ANTHROPIC_MODEL = "claude-opus-5"`, `backend/src/agent/config.py:23`).
    **Still stale:** `docs/tickets.json` T-21 names `OPENAI_API_KEY`, and `.env` still
    carries an `OPENAI_API_KEY` entry — both historical, neither read by the app.
14. ~~**`.env` is loaded by nothing.**~~ **FIXED by W1-F4, 2026-08-16, commit `972c13b`.**
    *Original finding, preserved:* No `load_dotenv()` anywhere in the app or scripts,
    so the documented run commands see zero Zendesk credentials even though `.env` is
    fully populated — `live_smoke.py` prints "credentials absent" and exits 0, and the
    webhook 500s. The deploy path works only because `docs/deploy.md:139` instructs
    `set -a; source .env; set +a` first. **Now:** `backend/src/main.py` and
    `backend/src/worker/main.py` both call `load_dotenv(..., override=False)` at startup.
    `docs/deploy.md`'s `set -a; source .env; set +a` is still required for
    `docker compose` itself, which resolves `${VAR}` before any Python runs.
15. ~~**`ZENDESK_AI_USER_ID` named a user the token never acts as — so both loop guards were
    inert at the same time.**~~ **FIXED 2026-08-17, verified live.** It was configured as
    `54404962250395` ("Othram AI Agent", role agent), but the OAuth token acts as
    `54402664002843` ("Hank Holcomb", admin) — which is who actually authored the AI's reply.
    Ingress's self-event drop was therefore comparing against an id that never appears in any
    event. **Combined with §5.1 the loop had no guard at all**, and the AI's own public comment
    satisfies the trigger's "Comment is Public" condition. Zendesk's invocation log confirms
    the AI's replies genuinely *did* re-fire the trigger (502s at 06:39 and 06:57, from when
    the tunnel was misrouted). **The only thing that prevented an infinite reply loop was a
    third, unrelated bug: `{{ticket.latest_comment_id}}` renders *empty* in this account, so
    the follow-up webhook deduped against the earlier `(ticket_id, '')` row. A coincidence
    held the guard, not the design.** **Now:** `ZENDESK_AI_USER_ID=54402664002843`, verified
    live against the actual comment author; `verify_ai_user_id()` is a preflight that **raises**
    rather than warns; and a per-reply author read-back logs ERROR on drift. *Open owner
    decision, not resolved:* every customer-visible reply is authored by **Hank Holcomb,
    admin** — see `docs/BUILD-PLAN.md §10.7b`.
16. ~~**No Zendesk trigger existed, so nothing ever fired the webhook.**~~ **FIXED
    2026-08-17.** The account had **7 active triggers and zero with a `notification_webhook`
    action**: the webhook existed, was active and pointed at the right URL, and nothing ever
    invoked it. Trigger **`54508374798747`** was created to `docs/zendesk-runbook.md` step 7's
    spec and verified by reading it back — the `ai-processed` nullifier in `conditions.all`,
    create-or-public-comment in `conditions.any`, and a JSON body matching
    `ZendeskWebhookPayload` on all six fields. This was an **undocumented Wave 4
    prerequisite**; it is now satisfied.
17. ~~**CI had failed 30 of 30 runs — every run in the repository's history.**~~ **FIXED
    2026-08-17.** Cause: a `backend/tests/hooks` test faked "no interpreter" with `PATH=/bin`,
    which is true on macOS and false on Ubuntu, where `/bin` is a usrmerge symlink into
    `/usr/bin`. **The guard was correct; the test was wrong about Linux.** That suite is
    retired (`docs/DECISIONS.md` ADR-019 amendment) and run **32003095488** shows
    `Lint ✓ Type-check ✓ Portal contract ✓ Test ✓` with **511 passed** — the first green suite
    on Ubuntu in this repo's history. **Two CI guards still cannot do their job** (owner's call,
    `docs/BUILD-PLAN.md §10.7c`): `ruff check .` in CI **silently skips 19 files** because
    `extend-exclude` is gitignore-style, so `backend/src/portal` and `backend/tests/portal`
    have never been linted in CI (they are clean today); and the **collected-count floor is 200
    against 511 actual**, so it would not notice losing 60% of the suite.

---

## 7. Ticket-harness state at retirement

30 of 32 tickets hold fingerprint-bound receipts in `.claude/evidence/` (tracked in git
since `9ed68e5`, so a fresh clone reports 30 resolved / 2 queued). **T-10 and T-11 are
the two queued**, and both are superseded by `docs/BUILD-PLAN.md` — see
`docs/DECISIONS.md` ADR-001.

Harness defects still open at retirement, recorded so they are not mistaken for closed:

- **W15 route 1** — a ticket's `verify` command can still write `docs/TASKS.md`,
  `.claude/NEEDS_HUMAN.md`, `.claude/monitor/**` and another session's claim record, and
  close clean.
- **ABSOLUTE is case-sensitive on a case-insensitive volume** — `.claude/Evidence/T-1.json`
  is the same inode as `.claude/evidence/T-1.json` and returns `allow` from the guard.
- **W8** — no "verified unachievable / superseded" terminal state exists.
- **W13** — authorisations asserted in commit bodies rather than in the escalation channel.
- **W18** — `61d26de` is titled "chore: regenerate docs/TASKS.md" but carries a 100-line
  `harness_lib.py` change. Pushed to both remotes; not rewritable without a force-push.

These are now historical. The build protocol that replaces the harness is
`.claude/rules/build-protocol.md`.

---

## 8. Baseline commands

```bash
uv run pytest -m "not live" -q      # full suite, ~2.5 min
uv run ruff check .                 # note: extend-exclude is gitignore-style, so a
                                    # directory named `portal` is skipped at ANY depth,
                                    # including backend/src/portal. Lint those explicitly.
uv run mypy backend
```

Traps that cost real time before — do not rediscover them:

- `backend/tests/data/test_concurrency.py` fails if a **third** pytest process touches
  the same database. Never run the suite while a subagent runs it too.
- A new **top-level** file under `backend/tests/` that imports `data.db` lands in
  T-1/T-8/T-20/T-24's reverse-dependency set. Put new test files inside an existing
  suite directory.
- `uv run python -m evals.report` exits non-zero **by design** (the approval gate) and
  refuses to write under `docs/` while unapproved. Pass `--output-dir` elsewhere for a
  draft.
