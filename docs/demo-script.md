# Demo script / shot list (T-11)

> **Read this section first.** SPEC success criterion 1 requires all five
> scenarios below to run "end-to-end against the live Zendesk trial, on
> camera." **As of this writing, none of them can be recorded.** The
> reason has changed, though, and narrowed: a Zendesk trial *has* been
> signed up for and `docs/zendesk-runbook.md` *has* been followed, so
> `ZENDESK_SUBDOMAIN`, `ZENDESK_OAUTH_TOKEN`,
> `ZENDESK_WEBHOOK_SIGNING_SECRET` and `ZENDESK_AI_USER_ID` are all set in
> `.env` — but the token obtained then has since **expired (HTTP 401)**,
> with no refresh token stored, so it needs a fresh `authorization_code` +
> PKCE consent (`uv run python scripts/zendesk_oauth.py`) before any live
> call succeeds. The droplet is **no longer a blocker**: this project's
> droplet is live at `161.35.2.250` (`docs/deploy.md`). The Langfuse trace
> shot remains un-recordable for an independent reason: no Langfuse
> instrumentation exists in the code at all (see "Shot 8" below). This
> document is written so it is **immediately usable once those gaps
> close** — it names exact UI actions and exact files to point at, not
> placeholders to fill in later.

Every scenario's *logic* is proven today by an equivalent test driving
the real LangGraph graph against `EmailAdapter` (an in-memory fake, not
Zendesk) — cited per scenario below so you can confirm the behavior is
real before spending camera time on it. That is not a substitute for the
live-Zendesk recording SPEC requires; it's how you'd sanity-check a
scenario is going to behave correctly before you burn a take on it.

## Prerequisites before any live shot

1. Follow `docs/zendesk-runbook.md` in full: trial signup, OAuth app +
   token, dedicated AI agent user, `cloudflared` tunnel (or a deployed
   droplet per `docs/deploy.md`), webhook + signing secret, trigger.
2. Confirm `.env` has all of `ZENDESK_SUBDOMAIN`, `ZENDESK_OAUTH_TOKEN`,
   `ZENDESK_AI_USER_ID`, `ZENDESK_WEBHOOK_SIGNING_SECRET` filled in.
3. Run `uv run python scripts/live_smoke.py <a test ticket id>` once
   first — it exercises every `HelpdeskPort` operation against the live
   trial and fails loudly if the `ai-processed` tag isn't landing (which
   would otherwise infinite-loop the webhook). Confirm it prints "all
   HelpdeskPort operations completed" before recording anything.
4. Seed fixture data if not already done:
   `uv run python -c "from data.seed import seed_all; print(seed_all())"`.
5. Start the backend (`uv run uvicorn main:app --app-dir backend/src
   --port 8000`) and portal (`cd portal && npm run dev`), or use the
   `deploy/` stack per `docs/deploy.md` if recording against a droplet.

## Shots 1–5: the five live scenarios

For each: create the ticket as the fictional customer in the Zendesk
end-user view (or the public support form), let the agent run, then show
the Zendesk agent view with the reply/note/tags/status visible — SPEC's
own phrasing is "with the Zendesk UI showing reply/note/tags/status," so
frame the shot to include the ticket's tag list and status dropdown, not
just the reply text.

### Shot 1 — status / no-escalation

Ask about an existing case's status as its real requester, e.g. "Hi, can
you tell me the current status of case MFG-2025-0734?" using a fictional
requester email seeded in `fixtures/cases.yaml`.

Expected: a public reply containing the real case id, stage, and ETA,
built entirely from `agent/templates.py:render_case_status_reply`; tag
`case-status` added; status set to `solved`; no internal note.

Non-live proof today:
`backend/tests/graph/test_canonical_scenarios.py::test_case_status_question_resolves_to_public_reply_with_real_case_facts`.

### Shot 2 — permission / no-escalation

Ask for something on the closed always-grant list as the real requester,
e.g. "Can you add my spouse as an authorized contact on my case?"

Expected: a public reply confirming the grant
(`render_permission_grant_reply`), stating no case-specific fact; tag
`permission-granted`; status `solved`; no internal note.

Non-live proof today:
`test_canonical_scenarios.py::test_permission_request_under_always_grant_is_granted_and_solved`.

### Shot 3 — complex/technical / escalation

Ask something the KB genuinely can't ground, e.g. "What exact
demineralization chemistry do you use on degraded skeletal extracts, and
how does that interact with heteroplasmy rates in downstream mtDNA
variant calls?" — routed to `kb`, but the groundedness verifier should
score it low and force escalation.

Expected: an internal note with the three headed sections
(`escalation/notes.py`), tags including `escalated` and `low_confidence`,
assignment to the escalation group, status `open`, and a public reply
that is the fixed `templates.ESCALATION_CUSTOMER_REPLY` — never the raw
technical non-answer.

Non-live proof today:
`test_canonical_scenarios.py::test_complex_technical_question_fails_verifier_and_escalates`.
Note this scenario's real-model behavior is genuinely unverified in a
different sense than the others: the test above drives it with a
`FakeLLMClient` returning a canned low groundedness score, so it proves
the *escalation path* works when the score is low, but not that the real
`claude-opus-5` will actually score this particular question low. If the
real model scores it higher than expected during the live recording, the
KB answer might send instead of escalating — worth a dry run against the
real API before committing this to the final recording.
`ANTHROPIC_API_KEY` is already configured here, so that dry run can happen
at any time; it does not wait on the Zendesk re-auth.

### Shot 4 — off-topic / boundary

Ask something unrelated, e.g. "Do you sell dog food?"

Expected: the fixed polite redirect (`templates.OFF_TOPIC_REPLY`); tag
`off-topic`; status stays `open` (never `solved` — R5's explicit
boundary).

Non-live proof today:
`test_canonical_scenarios.py::test_off_topic_message_gets_polite_redirect_and_stays_open`.

### Shot 5 — adversarial unknown-case

Reference a case id that doesn't exist, e.g. "What's the status of case
MFG-0000-0000?"

Expected: escalation with **zero leaked case facts** — no template
interpolation happens because no `Case` was ever resolved; internal note
naming `unknown_case`; the same fixed customer-facing escalation copy as
Shot 3.

Non-live proof today:
`backend/tests/grounding/test_adversarial.py::test_unknown_case_id_never_invents_facts_and_escalates`
— this is also the closest existing test to R9's adversarial suite this
scenario is meant to demonstrate live; see `docs/grounding.md` for the
rest of that suite (false-premise assertions, cross-requester lookups,
prompt injection) if you want additional adversarial angles beyond the
one SPEC names.

## Shot 6 — gate flip ON, review queue, edited-approve send

This shot does **not** require live Zendesk — it works against the local
stack today (backend + portal + Postgres, `EmailAdapter` or
`ZendeskAdapter` either one) and is a good one to rehearse now.

1. In the portal (`portal/src/App.tsx`), show the gate toggle
   (`components/GateToggle.tsx`) currently OFF — copy reads "Gate is OFF —
   replies send autonomously."
2. Flip it ON (`PUT /api/settings/gate`) — copy updates to "Gate is ON —
   every reply is held as a draft for approve/edit/reject."
3. Trigger a scenario (e.g. Shot 1's status question) — this time the
   agent run persists a `pending` draft (`agent/nodes.py:act`, gate-ON
   branch) instead of sending. Show the feed
   (`components/Feed.tsx`) picking it up on its next 5-second poll.
4. Open the draft (`components/DraftDetail.tsx`). Show the route,
   confidence, and draft body.
5. **Edit the reply text** in the textarea, then click "Approve edited
   reply." This does two things worth narrating on camera:
   `PUT /api/drafts/{id}` persists the edited text first, *then* `POST
   /api/drafts/{id}/approve` sends exactly that edited text via the
   `HelpdeskPort` — the ordering matters (`DraftDetail.tsx`'s own comment
   calls this out: skip the PUT and an edit silently never reaches the
   customer).
6. Show the run's outcome recorded as `gated_sent` — and, in the metrics
   panel (Shot 7 below), that this send did **not** move the
   human-avoidance numerator, only the denominator (R12).

## Shot 7 — metrics panel

Also works locally today. Show `components/MetricsPanel.tsx`:
human-avoidance rate, latency p50/p95, and escalations-by-reason. Worth
narrating: after Shot 6's gated-approve send, the human-avoidance rate
should visibly *not* increase from that send — it only counts autonomous
(`auto_sent`) outcomes in the numerator, while `gated_sent` still counts
in the denominator (both computed in `backend/src/portal/service.py:compute_metrics`).
If several escalations have accumulated by this point in the demo, the
escalations-by-reason list is also worth showing next to the internal
notes from Shots 3/5 — the reasons should visibly match.

## Shot 8 — one Langfuse trace: tool result → templated reply

**Currently un-recordable — this is a code gap, not a missing credential.**
`agent/nodes.py:act` mints `trace_id` as a bare `uuid.uuid4().hex` and
never reports anything to Langfuse — there is no Langfuse SDK call
anywhere in this codebase. `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
exist in `.env`, but nothing reads them for tracing. The portal's
`trace_url` field (`backend/src/portal/service.py:_trace_url`)
constructs a conventional-shape URL
(`{LANGFUSE_HOST}/trace/{trace_id}`) from that bare uuid, but it does not
resolve to a real trace — clicking it in the portal today leads to a
Langfuse 404, not a trace page. This is documented in that function's own
docstring as "a secondary, non-blocking gap."

**What this shot needs before it can be recorded:** actual Langfuse
instrumentation added to the graph (e.g. wrapping `agent.graph.run_agent`
or individual `LLMClient.structured` calls with the Langfuse SDK/decorator
so a run's `classify`/`compose`/`verify` calls appear as spans under one
trace), reporting the *same* `trace_id` `act` already mints so the portal
link stays consistent. Once that exists, the shot itself is
straightforward: run any resolving scenario (Shot 1 is simplest), open
the resulting trace in the Langfuse UI, and show the tool-result span
(the `Case` object `case_status` resolved) feeding directly into the
templated `compose` output — i.e., visually confirm the R9 story
(`docs/grounding.md`) at the trace level, not just in test assertions.

## Summary: what's recordable right now vs. blocked

| Shot | Recordable today? | Blocker |
|---|---|---|
| 1–5 (five live scenarios) | No | Zendesk re-auth **and** the ingress→agent wiring below. Droplet is no longer a blocker |
| 6a (gate flip) | **Yes** — verified in a real browser against the droplet | None — rehearse now |
| 6b (edited-approve) | No | There are no drafts. `runs` is empty on the droplet *and* locally |
| 7 (metrics panel) | Renders, but empty | Same cause: 0 runs, so every figure is 0 and "No escalations recorded" |
| 8 (Langfuse trace) | No | No Langfuse instrumentation exists in the code |

**Corrected after actually opening the portal in a browser.** An earlier
version of this table said shots 6 and 7 were "genuinely ready to record
today and don't need to wait on anything." That was too optimistic, and a
later edit made it worse by adding "or the droplet." What is true:

- **Shot 6a is real and verified.** Clicking the gate checkbox on the
  droplet issues `PUT /api/settings/gate` → 200, and the server then
  reports `{"enabled":true}`. The label flips to "Gate is ON — every reply
  is held as a draft for approve/edit/reject." That is filmable today.
- **Shot 6b and shot 7 are not.** Both need runs, and `runs` is empty
  everywhere. `seed_all` loads only `cases` and `kb_chunks`; nothing seeds
  runs. The metrics panel renders correctly but shows 0% / 0.0s / 0.0s and
  "No escalations recorded" — technically a working panel, not a demo.

The reason there are no runs is **not** the Zendesk token: the webhook
ingress validates, dedups and returns `202 accepted` without ever starting
an agent run, and `agent.graph.run_agent` has no production caller at all
(every call site is a test). Re-authorizing Zendesk gets tickets created
and webhooks accepted, and still produces no reply and no draft. See
`.claude/NEEDS_HUMAN.md` — connecting ingress to the agent is outside every
existing ticket's scope and needs a plan decision.

So shots 1–5 need two things, not one: the Zendesk re-auth
(`uv run python scripts/zendesk_oauth.py`, per `docs/zendesk-runbook.md`)
**and** that wiring. Shot 8 needs Langfuse instrumentation before it exists
to record at all.

One presentational note, since the camera will show it: the portal has no
stylesheet at all — no `.css` file, no CSS import, no `className` anywhere
in `portal/src/`, and the browser requests no stylesheet. It renders as
unstyled default-serif HTML. T-9's non-goal capped styling at
"clean-and-readable" so this breaks no acceptance criterion, but it is
worth a deliberate decision before filming rather than a surprise.

See `docs/architecture.md` for the full pipeline these scenarios exercise,
`docs/grounding.md` for the adversarial story Shot 5 is a live instance
of, and `docs/escalation.md` for what Shot 3's internal note actually
contains.
