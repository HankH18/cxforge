# Handoff

State as of commit `78ebdcb`. Only things a new agent cannot infer from the
repo, the harness, or CLAUDE.md.

## Where the build actually is

T-0 through T-6, T-8, T-9 are done and verified. T-7 and T-11 are claimed but
**incomplete**; T-10 has never run. `.claude/evidence/T-N.pass` records which
tickets have a genuinely passing verify — T-7, T-10 and T-11 have no entry.

A **priority batch T-12..T-21** was added to `docs/tickets.json` after the
original graph was built (remediation raised from observed defects). Those
carry `"priority": "next"` and are claimed BEFORE the ascending-ID default.

## Zendesk is now fully wired and live-verified

All six values in `.env` are populated and confirmed against the real trial:
subdomain, OAuth client id, OAuth token (verified via `GET /api/v2/users/me`),
AI agent user id, and the webhook signing secret.

- **AI agent user**: `Othram AI Agent <hank.holcomb@challenger.gauntletai.com>`,
  id `54404962250395`, role agent. Distinct from the admin, which is what makes
  attribution and the self-event drop meaningful.
- **Webhook**: name `Jarvis`, id `01KZZFR8MFA0GNPKCP0F5WJWEM`, active.
- **OAuth client**: `jarvis`, id `54402934189339`, `kind: "public"`.

**Still empty**: `OPENAI_API_KEY` (nothing needs it to pass today; required for
live agent runs and T-7's real classifier measurement), and `DEPLOY_HOST` (no
droplet exists). `DIGITALOCEAN_ACCESS_TOKEN` works — account active, two
UNRELATED droplets already running, do not touch them.

## Ephemeral state that dies with the session

A `cloudflared` quick tunnel and a `uvicorn` dev server on `:8000` were running
when this was written. **The tunnel hostname is random and does not survive a
restart.** The Zendesk webhook currently points at
`https://exhibits-rise-consortium-news.trycloudflare.com/webhooks/zendesk`,
which will be dead. Before any live work: restart both, then update the
webhook's endpoint (`PATCH /api/v2/webhooks/{id}`) to the new hostname. The
webhook path is always `/webhooks/zendesk`.

## Two Zendesk traps, both of which cost hours

**1. Public OAuth clients require PKCE.** The client is `kind: "public"`, which
cannot hold a secret, so `code_challenge` + `code_challenge_method` are
mandatory. Without them `/oauth/authorizations/new` returns `invalid_request —
missing a required parameter` even when client id, secret and redirect URL are
all correct. Four wrong diagnoses preceded finding this.

**2. The webhook signing secret is not shown at creation.** The current Zendesk
flow creates the webhook, connects it to a trigger, and reveals the secret only
afterward — via "Reveal secret" in the UI or
`GET /api/v2/webhooks/{id}/signing_secret`.

**The general lesson: read the object's real config instead of guessing.** From
a logged-in browser tab, or with the OAuth token:
```js
await fetch('/api/v2/oauth/clients/54402934189339.json', {credentials:'include'}).then(r=>r.json())
```
One such call would have ended each of the above immediately. Error semantics:
`invalid_client` = credentials wrong; `invalid_grant` = credentials ACCEPTED,
the code or redirect is the problem.

Two other silent config traps already fixed: `ZENDESK_SUBDOMAIN` must be the
**bare name** (it had `.zendesk.com`, doubling the API base URL), and
`client_id` is the client's **unique identifier**, not its numeric id.

## The signature bug — the most important thing in this document

First contact with a real webhook found that `compute_signature`
base64-decoded the signing secret before using it as the HMAC key. Zendesk's
secret is a 44-character string that is not valid base64, so **every genuine
request failed closed with 401 before the signature was even compared.** The
webhook could never have worked.

The suite could not catch it. The fixture was itself `base64.b64encode(...)`,
so it decoded happily, and the test helper reimplemented the same wrong
assumption. Implementation and tests agreed with each other; neither agreed
with Zendesk. **222 green tests proved internal consistency and nothing about
reality.** The fixture is now a 44-char deliberately-non-base64 string so a
reintroduction fails.

Treat this as the template for what remains unproven: every contract test is
mocked, and no code in this repo had ever talked to real Zendesk until this
point. Ingress is now verified live (valid→202, duplicate→202 `duplicate:true`,
new comment_id→202, AI-authored→202 dropped, bad signature→401, malformed→400).
**`ZendeskAdapter`'s write path is still entirely unproven against reality** —
`scripts/live_smoke.py <ticket_id>` is the tool for that, and it writes to a
real ticket, so get the owner's nod first.

## Verification posture — what green does and does not prove

- **No end-to-end agent run has happened.** R8's "p95 < 5 min" is UNMEASURED.
  Do not repeat a latency number; none exists.
- **The eval report is a DRAFT.** T-7 labels are `PROPOSED_AWAITING_HUMAN_REVIEW`.
  `evals/report.py` refuses to emit a non-draft while unapproved, with a test
  proving that refusal is a real branch. The classifier half is STUBBED; only
  billing / human_request / unknown_case hard rules are measured for real.
  **Never self-approve those labels** — a system supplying its own ground truth
  measures nothing, which is the entire point of the gate.
- **Langfuse keys are set but there is NO instrumentation.** `trace_id` is a
  bare uuid4 that is never reported, so `trace_url` does not resolve. The demo
  shot list calls for a Langfuse trace — that shot is currently impossible.
- The deploy stack is verified LOCALLY only. No droplet has ever run it.

## Design invariants that were attacked and must not regress

- **R9 grounding.** A red-team pass proved the KB free-generation branch could
  emit a fabricated case fact while the LLM groundedness judge self-scored it
  1.0 — generator and judge are the same client.
  `backend/src/agent/grounding_guard.py` is a deterministic, no-LLM backstop
  that forces escalation regardless of that score. Shape-based, not semantic;
  residual paraphrase risk documented in its docstring. Do not "simplify" it
  into a prompt instruction.
- **R12 metric.** Gated sends sit in the denominator and are excluded from the
  numerator. The fixture is built so the correct reading (0.625) and the
  flattering one (0.75) differ. Do not "fix" the numerator.
- **R6 classifier reachability.** The escalation classifier was once fully
  implemented, unit-tested, and *never called by the live graph* — dead code in
  production while the suite was green. When touching `decide`, confirm
  reachability, not just unit coverage.

## Scope-graph friction (recurring, expect more)

Three tickets could not satisfy their own acceptance criteria without a scope
amendment, because a file they had to edit belonged to a closed ticket: T-6
(needed T-5's graph fakes), T-8 (needed the `runs` schema for R13
`escalations_by_reason`), T-0/T-11 (`.env.example` had no DigitalOcean vars).
When a ticket seems structurally unable to finish, check whether the file it
needs sits in another ticket's scope before assuming the implementation is
wrong — and amend `docs/tickets.json` explicitly rather than working around it.

`docs/DESIGN.md` has been amended twice where it contradicted itself: the
webhook payload gained `comment_author_id` (the pinned payload had no author
field yet the same paragraph required dropping AI-authored events), and `runs`
gained `reasons text[]`. Both are annotated in place.

## Next actions

1. **Verify the Zendesk trigger** — it must fire the `Jarvis` webhook, carry the
   `tags not include ai-processed` nullifier, and send a JSON body whose keys
   match `ingress.models.ZendeskWebhookPayload` field-for-field including
   `comment_author_id: {{current_user.id}}`. Read it via
   `GET /api/v2/triggers` rather than inspecting the UI. This was never
   confirmed.
2. Restart tunnel + app, repoint the webhook endpoint, then **T-10**: the first
   real end-to-end run and the only source of an R8 latency number.
3. Owner reviews `evals/REVIEW.md` (5 genuinely contestable frustration/
   complexity labels) → T-7 closes.
4. Droplet + `DEPLOY_HOST` → T-11's deploy half → recording.
5. The T-12..T-21 priority batch is claimable at any point.
