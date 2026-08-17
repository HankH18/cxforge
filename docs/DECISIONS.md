# Decision record — the last mile

Decisions taken by the project owner (Hank Holcomb) on **2026-08-16**, after the
verified audit recorded in `docs/STATE.md`. Each entry states the decision, the reason it
needed a human, and what it commits us to.

These supersede any conflicting instruction in `docs/HANDOFF.md`,
`.claude/NEEDS_HUMAN.md`, or `.claude/rules/harness-protocol.md`.

---

## ADR-001 — The cc-factory build harness is retired for the last mile

**Decision.** Stop the claim/close/receipt lifecycle at 30 of 32 tickets. Finish the
remaining work as ordinary engineering with ordinary commits, gated by the full test
suite rather than by per-ticket receipts. **T-10 and T-11 are superseded by
`docs/BUILD-PLAN.md`** and will not be claimed or closed.

**Why it needed a decision.** The remaining work was structurally unownable. The
ingress→agent wiring is in no ticket's scope; T-4 owns the file and disclaimed the job,
T-5 cannot reach ingress, and T-10/T-11 cannot touch `backend/src`. Worse, *adding* a
ticket requires regenerating `backend/tests/plan/ticket_structural_snapshot.json`, which
is in no ticket's scope either — so a plan amendment could not be completed by any single
claim. The harness had reached a state where it could not describe the work that
remained.

**What this commits us to.** `.claude/evidence/` is preserved untouched as the historical
attestation record for the 30 tickets that did go through the lifecycle — that record is
real and stays real. The 5 open harness defects listed in `docs/STATE.md §7` become
historical rather than outstanding. The replacement working agreement is
`.claude/rules/build-protocol.md`; its gate is the full suite plus lint plus types, run
before every commit.

**Rejected.** Owner-committed ticket amendments (keeps attestation, but every remaining
unit would need you in the loop for a JSON edit before an agent could start). A one-off
written authorization (fast, but ships the last mile with no receipts anyway — the cost
of the harness without its benefit).

---

## ADR-002 — Dispatch is Redis + arq, in a dedicated worker service

**Decision.** The webhook enqueues a job; a separate `worker` container consumes it and
calls `run_agent`. Broker is Redis; the job framework is **arq**.

**Why it needed a decision.** Three viable shapes with different failure modes.
Synchronous-in-handler would hold Zendesk's connection through 20–60s of model calls and
serialize all intake. `BackgroundTasks` was cheap here (`run_agent` is a sync callable so
Starlette threadpools it, and `get_connection()` opens a fresh psycopg connection per
call, so the usual DB-session-lifetime objection does not apply) but leaves failures
invisible and offers no retry surface.

**What this commits us to.** A 4th and 5th service in `docker-compose.yml` (`redis`,
`worker`), an `arq` dependency, and a job contract frozen in `docs/BUILD-PLAN.md
§Contracts`. The pinned `202` response and the **8 `== 202` assertions across 6 tests** in
`backend/tests/ingress/test_webhook.py` are **preserved** — a failed run never changes what
Zendesk sees. Redis is deliberately not
used for anything else; Postgres stays the system of record.

---

## ADR-003 — A failed run releases its dedup row

**Decision.** On an exception inside the run, `DELETE` the `(ticket_id, comment_id)` row
from `tickets_seen` and log at ERROR.

**Why it needed a decision.** The dedup row is committed *before* dispatch and the table
carries only the two id columns, so as built, a run that raises means that customer
comment is dead forever — recoverable only by a manual `DELETE`. During filming, one
transient Anthropic 529 would silently kill a scenario.

**What this commits us to.** Reprocessing is possible by re-firing the Zendesk trigger,
with no migration and no new columns. It also means a *persistently* failing ticket can
loop if the trigger re-fires repeatedly — acceptable at demo volume, and the ERROR log is
the signal. We explicitly did **not** add `status`/`attempts` columns; that is the
correct production answer and the wrong one for this deadline.

---

## ADR-004 — Latency is measured from true webhook receipt

**Decision.** Stamp receipt time in the ingress handler, carry it on the job payload, and
have `act` use the injected value instead of `datetime.now(UTC)`. `runs.received_at`
comes to mean what `docs/DESIGN.md` (§ *Latency*, "webhook receipt → public reply posted")
has always said it means. Cited by section rather than line number deliberately: W0.3 grew
DESIGN.md by 209 lines and broke every inbound line citation in this file and in
`docs/STATE.md`.

**Why it needed a decision.** As built, `received_at` is minted at `nodes.py:591` —
inside `act`, the *last* graph node — so R13's p50/p95 time only the Zendesk API calls
and exclude every model call. Fixing it properly would normally cross four closed
tickets' scopes; ADR-001 removes that obstacle, and ADR-002's job row is the natural
carrier, which makes this nearly free.

**What this commits us to.** `/api/metrics` and the scenario runner's externally-measured
stopwatch must agree. The false half of the docstring at `portal/service.py:307-311` gets
deleted rather than reworded.

---

## ADR-005 — Webhook transport is a Cloudflare named tunnel

**Decision.** `cloudflared` runs a **named** tunnel bound to a domain the owner controls.
Owner sets up Cloudflare DNS. No inbound ports are opened on the droplet.

**Why it needed a decision.** Zendesk requires HTTPS and the droplet has no TLS at all —
443 and 8443 refuse connections, port 80 times out. A quick-tunnel would work but its URL
changes on every restart, forcing a re-paste into Zendesk Admin Center before every take.

**What this commits us to.** A stable hostname for the whole demo, no certificate
management, and the droplet's origin ports can stay closed. `docs/deploy.md` and
`docs/zendesk-runbook.md` are rewritten against the named-tunnel flow, and the tunnel runs
under a supervisor so it survives a reboot.

---

## ADR-006 — Langfuse is instrumented for real, in a new `cxforge` project

**Decision.** Instrument the graph so `classify → compose → verify` appear as spans under
the `trace_id` that `act` already mints, keeping the portal's trace link consistent.
Traces land in a **new Langfuse project named `cxforge`**.

**Why it needed a decision.** Langfuse is named in SPEC's constraints and demanded by
T-11 acceptance 3, but there is not one `import langfuse` in the repo and the portal's
trace URL is built from a bare `uuid4` that would 404. Separately, `.env` holds the
**identical `sk-lf-…` value in both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`** —
no public key is configured — and the keys resolve to the owner's personal `jarvis`
project, which would put unrelated traces in the demo trace list.

**What this commits us to.** The owner creates the project and pastes a correct
`pk-lf-…` / `sk-lf-…` pair (see `docs/OWNER-ACTIONS.md`). This is the single best visual
for the zero-hallucination story: a grader watches the `Case` tool result feed the
template.

**Rejected.** A self-hosted trace view (honest, no account, but not Langfuse — needs a
SPEC erratum). Self-hosting Langfuse in compose (needs Postgres + Clickhouse; too much,
too late).

---

## ADR-007 — The eval report is regenerated with provenance and the sweep

**Decision.** Add a model identifier and the full threshold sweep to `metrics.json`,
re-run live, and publish whatever the numbers actually are. State the measured basis
(10 of 16 hard-trigger tickets) explicitly next to the headline recall.

**Why it needed a decision.** Regenerating costs ~37 live Opus calls and is
non-deterministic — the published 1.000 could move, and the artifact carries a human
approval.

**What this commits us to.** If the numbers move, the new numbers ship. The report gains
one honest sentence about what P=R=F1=1.000 does and does not mean on 45 tickets whose
labels and engine were authored in the same build. The 0.00-vs-0.50 threshold question
becomes answerable from the stored artifact instead of unfalsifiable.

---

## ADR-008 — Embeddings move to Voyage AI

**Decision.** Replace `HashingEmbedder` with a `VoyageEmbedder` behind the existing
embedder seam. Reseed the KB.

**Why it needed a decision.** Anthropic has no embeddings API, so this determines whether
an OpenAI dependency returns to a codebase whose entire isolation-layer story is the
Anthropic pivot.

**What this commits us to.** One new API key and a coherent provider story: Anthropic for
generation, their recommended partner for embeddings. Model is **`voyage-4-lite` with
`output_dimension=1024`** (verified against Voyage's model reference 2026-08-16) — the
`voyage-4` line takes a configurable dimension, and pinning 1024 matches the existing
`EMBEDDING_DIM` and the `vector(1024)` column, so this is a **reseed with no schema
migration**. Retrieval quality must be re-checked after the reseed, not assumed;
`HashingEmbedder` stays in the tree as a one-line revert and as the offline default that
keeps CI and the non-live suite network-free.

**Rejected.** Local sentence-transformers (no key, offline tests — but ~90MB into the
backend image and slower container start). OpenAI `text-embedding-3-small` (cheapest and
most conventional; reads as inconsistent with the pivot).

---

## ADR-009 — Customer history is implemented, not declared out of scope

**Decision.** Add `fetch_requester_history` to `HelpdeskPort`, implement it in both
adapters, extend the contract suite, and surface prior-ticket context to the classifier.

**Why it needed a decision.** It is the **only** PRD line item that appears in neither
the code nor SPEC's non-goals, and adding a port method changes a contract that
`port.py` explicitly says requires owner sign-off.

**What this commits us to.** Both adapters grow a method and the parametrized contract
suite covers it — `EmailAdapter` must satisfy it too (R14). A repeat complainer now reads
differently from a first-time asker, which is a real escalation-judgment improvement and
must be reflected in `docs/escalation.md`.

---

## ADR-010 — Retrieval gets a relevance floor

**Decision.** `search_kb` applies a score cutoff from config; below it, retrieval returns
empty.

**Why it needed a decision.** Without a floor, `search_kb` always returns chunks, which
makes R6's "empty retrieval" hard trigger **unreachable** — an escalation path the docs
describe can never fire. Choosing the threshold changes escalation rates.

**What this commits us to.** The floor must be calibrated against the reseeded Voyage
embeddings (ADR-008), not carried over from hashing similarity scores, and the chosen
value needs a recorded rationale.

---

## ADR-011 — The permission route stops overstating

**Decision.** Reword the permission reply from "has now been processed for your case" to
language that matches what the system actually does. Do not implement side effects.

**Why it needed a decision.** The reply asserts an action the codebase performs nowhere.
Implementing the side effects is a data-layer change; rewording is a line.

---

## ADR-012 — Portal gets a wireframe pass now, a design pass later

**Decision.** Build a solid **semantic wireframe** — proper markup with clean class hooks
across every component — plus a minimal stylesheet that reads as finished. A full design
pass happens later in a separate session and is out of scope here.

**Why it needed a decision.** The portal ships zero CSS. It breaks no acceptance criterion
(T-9 capped styling at "clean-and-readable") but it is the most visible thing in demo
shots 6 and 7.

**What this commits us to.** Effort goes into structure and class hooks, not visual
polish. The stylesheet should be small and easy to throw away.

---

## ADR-013 — A real promptfoo suite gets built

**Decision.** Wire `promptfooconfig.yaml` to actual assertions over the canonical
scenarios and the adversarial grounding set.

**Why it needed a decision.** SPEC's constraints and DESIGN's verification strategy both
name Promptfoo and DeepEval. `promptfooconfig.yaml` is still the T-1 scaffold with a
placeholder prompt and `tests: []`, promptfoo is not installed, and DeepEval is a
dependency with zero imports. The alternative was to delete both and amend SPEC.

**What this commits us to.** A second, independent evidence stream alongside
`evals/report.py`. DeepEval's status is settled in the same pass: either it does real work
or it comes out of `pyproject.toml` and out of SPEC.

---

## ADR-014 — SPEC and DESIGN are amended in place, with this record

**Decision.** Correct the provider references (and the other drift) directly in
`docs/SPEC.md` and `docs/DESIGN.md`, with this file as the decision record.

**Why it needed a decision.** Those files were read-only under the harness, which ADR-001
retires. The pivot is a *good* story — the `LLMClient` isolation layer worked exactly as
designed and swapping providers cost one module — and erasing it silently would throw
that away.

**What this commits us to.** The plan docs describe the system that exists. The history
lives here rather than in a stale doc a grader reads first.

---

## ADR-015 — Live e2e runs against the droplet at 20–30 tickets

**Decision.** `scripts/scenario_runner.py` drives the live Zendesk trial through the
Cloudflare tunnel to the **deployed droplet**, seeding 20–30 tickets so p50/p95 are
statistically meaningful.

**Why it needed a decision.** Five tickets satisfy SPEC success criterion 1 but give five
latency data points; a larger sample consumes more of a trial that lapses ~2026-08-27 and
more Anthropic calls per run.

**What this commits us to.** Success criterion 1 is demonstrated against the *deployed*
system, not localhost, and the metrics panel has real data for shot 7. The runner must be
re-runnable without exhausting the trial, and rate limits must be respected.

---

## ADR-020 — The exact-date tickets are case-status questions; the labels were wrong

**Decided 2026-08-16, during Wave 2.** Owner call. `docs/BUILD-PLAN.md §10.2` Gap 1 raised
it as "owner decision, not a subagent's"; this is the answer.

**Decision.** Three parts, one decision.

1. **The two labels move.** `esc-low_confidence-verifier_failure-exact-date-01` and
   `esc-low_confidence-verifier_failure-summed-timeline-01` in `evals/labeled_set.yaml`
   become ordinary `case_status` tickets — `expected_escalate: false`,
   `expected_reasons: []`. Each carries a `relabeled:` block in the file recording the
   measurement, the reasoning and the consequence, so a future reader never finds an
   unexplained label diff.
2. **Case-status replies that state an ETA carry a qualifier.** `agent/templates.py`'s
   `render_case_status_reply` now ends its estimate with *"— an estimated timeline, and
   subject to change."* Scoped to the branch that actually states a forward-looking
   estimate; a completed case has no timeline to hedge and gets nothing.
3. **The `CLASSIFY_SYSTEM` prompt is NOT rewritten.** §10.2 offered rewording the prompt
   as the alternative fix. It is rejected: bending the classifier away from a reading the
   owner agrees is correct, in order to preserve a label the owner agrees is wrong, is
   fixing the measurement instead of the thing measured.

**Why it needed a decision.** W1-E3 (`evals/route_accuracy.py`, 2026-08-16, `claude-opus-5`
over all 51 labels, ~$0.30) drove the **shipped** `agent.nodes.classify` and found these two
tickets routed to `case_status` at **0.92 confidence** each — reproduced independently by
promptfoo. The labels expected them on the `kb` route, failing the groundedness verifier.
Someone was wrong, and the choice is not a coding decision: *"can you tell me the EXACT
calendar date my results will be ready?"* is, on any plain reading, a question about the
state of that customer's case. So the labels were wrong.

But the worry behind the original labels is real. The lab publishes per-stage windows, not
calendar dates, and `fixtures/kb/turnaround-times.md` explicitly warns against summing
stage windows. Answering an exact-date question with a bare week figure implies a precision
nobody has. That is a **reply-content** problem, not a routing problem, so it is fixed in
the reply.

**What this commits us to.**

- **The published eval numbers will move, and that is in policy, not an accident.** These
  two tickets currently sit in the escalation set behind `docs/eval-report/`'s
  `P = R = F1 = 1.000` and its hard-trigger recall. Removing them changes that denominator,
  and they were also two of the six tickets `evals/report.py` excludes as structurally
  unmeasurable — so `measured_sample_size` moves too (45 → 47 of the labeled rows).
  **ADR-007** already commits this project to regenerating the report live and publishing
  whatever the numbers are; that regeneration is Wave 3 **G1**. Nothing under
  `docs/eval-report/` was touched by this ADR, and `uv run python -m evals.report` was not
  run against it.
- **The verifier-failure hard trigger keeps a representative.** Both relabeled tickets were
  the labeled set's only `verifier_failure` examples, and
  `backend/tests/evals/test_labeled_set.py::test_every_low_confidence_subtype_is_covered`
  requires each of `low_confidence`'s three subtypes to be represented. Rather than weaken
  that test, one replacement was written —
  `esc-low_confidence-verifier_failure-summed-stages-01`, the same trap posed about the
  **published** stage windows with no case in it, so nothing about its phrasing invites a
  `case_status` reading. The labeled set is now 52 tickets: 32 branch-route, 20 escalate.
  `backend/tests/evals/test_route_accuracy.py`'s pinned counts were re-derived accordingly
  — that file's own comment demands they be changed deliberately, and this is that.
- **The two relabeled tickets keep their `esc-…` ids.** They are the join key into
  `evals/route-accuracy/results.json` and the approved `docs/eval-report/`, neither of
  which this change rewrites; renaming would orphan the measurement that justifies the
  decision. Recorded as an explicit exception in `labeled_set.yaml`'s `meta.id_convention`.
  Nothing keys off the prefix — every id-marker check gates on `low_confidence` being in
  `expected_reasons` first, and these two now have none.
- **The qualifier must never grow a fact.** It states no number, no stage, and no cause.
  `agent/grounding_guard.py` flags exactly that shape of claim and R9 is this project's
  headline property, so a disclaimer that explained *why* the estimate might slip ("due to
  lab throughput") would trip it and would deserve to.
  `backend/tests/grounding/test_reply_wording.py` pins this by comparing the guard's full
  extraction signature over the reply with and without the qualifier, not merely by
  asserting no violations.

**Rejected.** Rewording `CLASSIFY_SYSTEM` (see decision 3). Leaving the labels alone and
accepting a permanent known 2-ticket miss (it would make the route-accuracy harness report
a defect that is really a labeling error, forever). Adding the disclaimer to *every*
case-status reply (a completed case has no estimate; blanket hedging teaches customers to
skip the words where they matter).

---

## ADR-019 — The three `settings.json` acceptance tests are retired with the hooks they assert

**Decided 2026-08-16, during Wave 1.** Owner call, escalated rather than decided by the agent.

**Decision.** Retire exactly three test functions to `.claude/harness-archive/`, and **keep
all 326 others** in `backend/tests/hooks/`:

- `test_scope_guard_pathless_and_notebook.py::test_settings_json_matcher_includes_notebookedit`
- `test_scope_guard_fail_closed.py::test_notebook_edit_is_matched_by_settings_json`
- `test_close_unattributed_claim_gap.py::test_no_pretooluse_hook_matches_bash_tool_calls`

**Why it needed a decision, and why the agent escalated.** All three read the **live**
`.claude/settings.json` and assert the `PreToolUse` scope-guard hooks are installed. W0.2
removed those hooks — an owner-approved change implementing ADR-001. So all three now fail
with `KeyError: 'PreToolUse'`, plus one collateral failure in
`test_skip_db_tests_relocation.py`, which spawns a child pytest over that directory and
asserts `returncode == 0`.

The `justify-test-edit` gate refused this edit twice over, correctly. Its discriminating
question — *"would this test still be wrong if I reverted my change?"* — answers **no**:
restore the hooks and all three pass. And both carry docstrings identifying them as T-27
**acceptance criteria** ("Acceptance 3's other half"), which that gate says are never edited
by an agent, only escalated. Hence this ADR.

**Why three and not the whole directory.** 326 of the 329 tests exercise the guard *scripts'*
logic against synthetic fixtures — claim format, ledger integrity, evidence binding,
fail-closed behaviour, the stop guard, the verify gate. None of them depend on whether the
hooks are installed, and all still pass. Only these three read live configuration. Archiving
the directory wholesale would discard working coverage to solve a three-test problem.

**What this commits us to.** Nothing now asserts the guard hooks are absent, so a future
session could silently re-add them. That is acceptable: `.claude/rules/build-protocol.md` is
the working agreement and ADR-001 is the record. The archived file states the invariant in
prose for anyone who revives the harness.

---

## ADR-018 — `backend/tests/plan/**` is retired with the harness it tests

**Decided 2026-08-16, during Wave 1.** Owner call, resolving `docs/BUILD-PLAN.md §10.1`.

**Decision.** Move the 92 tests in `backend/tests/plan/**` out of the collected suite and
preserve them as a historical record, exactly as ADR-001 preserved `.claude/evidence/`.
Nothing is deleted. This also resolves §10.1's first question — whether `worker` should join
`FIRST_PARTY_ROOTS` — by dissolving it: there is no longer an import graph to be blind.

**Why it needed a decision.** ADR-001 retired the ticket harness, but this suite still gated
on `docs/tickets.json` **unfiltered by status**, so the frozen contracts of 30 closed tickets
still dictated where new code could live. It was not theoretical: three of four Wave 1 tracks
hit it. Track A's plan literally specified `backend/tests/worker/`, which would have turned up
to 11 closed tickets red; Track F lost real design time when an entirely natural
`import main` in a deploy test did turn 11 red, and had to restructure around it. Meanwhile
the check had gone blind to `ingress → worker`, the most important new edge in the repo,
because `worker` is in neither `FIRST_PARTY_ROOTS` nor `KNOWN_PACKAGES` — 92 green tests not
looking at the thing that mattered.

**Why this is not weakening a test.** `.claude/rules/build-protocol.md` rule 7 forbids
weakening a test to make failing code pass. These tests were not failing; they were
constraining. More importantly, **the rule they enforce is now subsumed by the gate.**
`test_blast_radius.py` existed to guarantee that a *ticket's narrow verify command* ran every
suite its scope could break. ADR-001 replaced narrow per-ticket verifies with the **full
suite before every commit** (rule 2, ADR-016). With no tickets and a full-suite gate, there is
nothing left for it to protect. Retiring it loses no coverage of the product — these suites
test the plan, not the software.

**What this commits us to.** The suite drops from 702 to ~610 and that number is stated
honestly wherever it appears; CI's floor of 200 is unaffected. `backend/tests/hooks/**` is
the same category of harness-testing suite and is **deliberately left alone for now** —
retiring it was not part of this decision and would need its own. If the ticket harness is
ever revived, the archived suite comes back with it.

---

## ADR-017 — A failed *enqueue* returns 500, so Zendesk retries

**Decided 2026-08-16, during Wave 1.** Owner call.

**Decision.** When the webhook handler cannot enqueue the job — Redis down, broker
unreachable — the endpoint returns **500**, not 202. The dedup row is still released
(ADR-003) so the retry is not swallowed as a duplicate.

**Why it needed a decision.** `BUILD-PLAN §1.1` pinned `202` and ADR-002 said "a failed run
never changes what Zendesk sees", but neither covered a failed **dispatch**. Track A read it
literally and returned 202. The consequence: Zendesk does not retry a 202, so a broker
outage silently drops the customer's event — the only trace being an ERROR log nobody is
watching. During filming, one Redis hiccup would lose a scenario with no visible signal.

**What this commits us to.** The system is modelled as something that could actually run in
production, where dropping customer events is not acceptable. A failed *run* still returns
202 (ADR-002/003 unchanged — the agent got its chance and recovery is re-firing the
trigger); only a failed *dispatch*, where the work was never accepted at all, returns 500 and
leans on Zendesk's own retry. The distinction is deliberate: 202 means "we have it", and we
only say that when we actually do.

**Testing consequence.** The 8 `== 202` assertions across 6 ingress tests are preserved —
they cover the success and duplicate paths. The broker-failure test inverts from asserting
202 to asserting 500, and must fail if the handler goes back to swallowing the error.

---

## ADR-016 — Aggressive schedule, parallelized — verification is not what gets cut

**Decision.** Compress the calendar by running independent tracks concurrently, **not** by
reducing verification or dropping features. Everything in this decision record ships.

**What this commits us to.** The wave structure in `docs/BUILD-PLAN.md` exists to keep
concurrent tracks off each other's files. Contracts are frozen *before* Wave 1 so parallel
tracks build against a fixed interface instead of negotiating one mid-flight. The full
suite, ruff and mypy gate every merge — that gate does not move.
