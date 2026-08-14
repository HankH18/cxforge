# Handoff

State as of commit `ca9cc7c`. Only things a new agent cannot infer from the
repo, the harness, or CLAUDE.md.

## Where the build actually is

T-0 through T-6, T-8, T-9 are done and verified. T-7 and T-11 are claimed but
**incomplete**; T-10 has never run. `.claude/evidence/T-N.pass` records which
tickets have a genuinely passing verify — T-7, T-10 and T-11 have no entry.

A **priority batch T-12..T-21** was added to `docs/tickets.json` after the
original graph was built (remediation raised from observed defects). Those
carry `"priority": "next"` and are claimed BEFORE the ascending-ID default.
They are also the only claimable work right now, since T-7/T-10/T-11 are all
blocked on human steps.

## Credentials: what is live and what is not

`.env` exists, is gitignored, mode 600. Populated: Postgres, `PORTAL_TOKEN`,
Langfuse keys, `DIGITALOCEAN_ACCESS_TOKEN` (verified — account active, two
UNRELATED droplets already running, do not touch them), and
`ZENDESK_OAUTH_TOKEN` (183 chars, verified against `GET /api/v2/users/me.json`).

Still empty, all human steps:
- `ZENDESK_WEBHOOK_SIGNING_SECRET` — runbook step 6, shown once at webhook creation.
- `ZENDESK_AI_USER_ID` — no dedicated AI agent user exists yet; the only
  Zendesk user is the admin. Creating one needs an email the owner controls,
  so it was deliberately not invented. Until it exists the agent would post
  as the admin and ingress's self-event drop is inert (the trigger's
  `tags not include ai-processed` nullifier is still the primary loop guard).
- `OPENAI_API_KEY` — nothing needs it to pass today; needed for live agent
  runs and for T-7's real classifier measurement.
- `DEPLOY_HOST` — no droplet exists for this project.

## Zendesk OAuth: the trap that cost hours

The OAuth client is `kind: "public"`. **Public clients cannot hold a secret, so
PKCE is mandatory.** Without `code_challenge`, `/oauth/authorizations/new`
returns `invalid_request — missing a required parameter` even when client id,
secret and redirect URL are all correct. Four wrong diagnoses were made before
this was found (numeric-vs-string client id, expired code, unsupported
redirect URI, unregistered redirect URI).

**Read the client's real config instead of guessing** — from a logged-in
browser tab:
```js
await fetch('/api/v2/oauth/clients/54402934189339.json', {credentials:'include'}).then(r=>r.json())
```
That one call would have shown `kind: "public"` and a already-correct
`redirect_uri` immediately. `scripts/zendesk_oauth.py --serve` now handles the
whole flow including PKCE.

Also fixed along the way, both silent failure modes: `ZENDESK_SUBDOMAIN` must
be the **bare name** (it had `.zendesk.com`, which doubled into the API base
URL), and `client_id` is the client's **unique identifier**, not its numeric id.

Error semantics worth knowing: `invalid_client` = credentials wrong.
`invalid_grant` = credentials ACCEPTED, the code or redirect is the problem.

## Verification posture — what green does and does not prove

- **No live Zendesk run has ever happened.** R8's "p95 < 5 min" is UNMEASURED.
  Do not repeat a latency number; none exists.
- **The eval report is a DRAFT.** T-7 labels are `PROPOSED_AWAITING_HUMAN_REVIEW`.
  `evals/report.py` refuses to emit a non-draft while unapproved, and there is
  a test proving that refusal is a real branch. The classifier half is STUBBED
  (no API key); only billing / human_request / unknown_case hard rules are
  measured for real. **Never self-approve those labels** — a system that
  supplies its own ground truth measures nothing, which is the entire point of
  the gate.
- **Langfuse keys are set but there is NO instrumentation.** `trace_id` is a
  bare uuid4 that is never reported, so `trace_url` does not resolve. The demo
  shot list calls for a Langfuse trace — that shot is currently impossible.
- The deploy stack is verified LOCALLY only (`bash scripts/verify_deploy.sh`
  brings it up and asserts health/portal/401/200). No droplet has ever run it.

## Design invariants that were attacked and must not regress

- **R9 grounding.** A red-team pass proved the KB free-generation branch could
  emit a fabricated case fact while the LLM groundedness judge self-scored it
  1.0 — generator and judge are the same client. `backend/src/agent/
  grounding_guard.py` is a deterministic, no-LLM backstop that forces
  escalation regardless of that score. It is SHAPE-based, not semantic;
  residual paraphrase risk is documented in its docstring. Do not "simplify"
  it into a prompt instruction.
- **R12 metric.** Gated sends sit in the denominator and are excluded from the
  numerator. The test fixture is deliberately built so the correct reading
  (0.625) and the flattering one (0.75) differ. Do not "fix" the numerator.
- **R6 classifier reachability.** The escalation classifier was once fully
  implemented, unit-tested, and *never called by the live graph* — dead code
  in production while the suite was green. Fixed, with a live-graph test. When
  touching `decide`, confirm reachability, not just unit coverage.

## Scope-graph friction (recurring, expect more)

Three tickets could not satisfy their own acceptance criteria without a scope
amendment, because a file they had to edit belonged to a closed ticket:
T-6 (needed T-5's graph fakes), T-8 (needed the `runs` schema for R13
`escalations_by_reason`), T-0/T-11 (`.env.example` had no DigitalOcean vars).
When a ticket seems structurally unable to finish, check whether the file it
needs sits in another ticket's scope before assuming the implementation is
wrong — and amend `docs/tickets.json` explicitly rather than working around it.

`docs/DESIGN.md` has been amended twice where it contradicted itself: the
webhook payload gained `comment_author_id` (the pinned payload had no author
field yet the same paragraph required dropping AI-authored events), and `runs`
gained `reasons text[]`. Both are annotated in place.

## Next actions, in dependency order

1. Owner reviews `evals/REVIEW.md` (5 genuinely contestable frustration/
   complexity labels) → flips the fixture header to APPROVED → T-7 closes.
2. Owner completes runbook steps 3 and 6 (AI agent user, webhook secret) →
   T-10 unblocks → first live e2e and the only real latency measurement.
3. Droplet + `DEPLOY_HOST` → T-11's deploy half → recording.
4. Meanwhile: the T-12..T-21 priority batch is claimable now.
