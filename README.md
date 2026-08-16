# Othram AI Support Agent

An autonomous AI support agent (Gauntlet challenger project, PRD_02 /
Othram) that handles inbound tickets for a fictionalized forensic-genomics
lab: answers routine inquiries grounded in a knowledge base and a live
case-management system, and escalates to a human only when necessary. A
React review portal (draft feed + approval gate) ships alongside it.

Built by executing an approved task graph (`docs/tickets.json`, mirrored
for humans at `docs/TASKS.md`) against a fixed spec (`docs/SPEC.md`) and
design (`docs/DESIGN.md`) — see those files for the full intent and
contracts. This README is the entry point: quickstart, where the rest of
the documentation lives, and an honest account of what's actually been
verified versus what's still outstanding.

## Quickstart (verified from a clean clone)

Every command below was run end to end on a clean checkout while writing
this section — including bringing up the production `deploy/` stack in a
separate terminal at the same time, to confirm the two never collide (see
`deploy/docker-compose.yml`'s header comment for how).

```bash
cp .env.example .env            # fill in as each ticket's docs/*-runbook.md requires;
                                 # empty Zendesk/Anthropic values are fine to start —
                                 # see "Status" below for what that does and doesn't unlock

# 1. Dev database (Postgres 16 + pgvector) — the root docker-compose.yml
#    is dev-only, a single `db` service the test suite points at.
docker compose up -d db

# 2. Python deps
uv sync

# 3. Full test suite (222 tests as of this writing)
uv run pytest
uv run pytest -m contract -q    # HelpdeskPort contract suite only — both adapters
uv run ruff check .
uv run mypy backend

# 4. Run the backend app (dev mode — for the production image, see
#    deploy/Dockerfile.backend and docs/deploy.md instead)
PYTHONPATH=backend/src uv run uvicorn main:app --reload --port 8000
# -> GET http://localhost:8000/health  ==>  {"status": "ok"}

# 5. Run the portal (separate terminal; proxies /api to :8000 automatically
#    — see portal/vite.config.ts)
cd portal
npm install
npm run dev
# -> http://localhost:5173
```

Seeding the case/KB fixture data the portal and grounding logic read
(`data.seed.seed_all`, offline — no `ANTHROPIC_API_KEY` needed, see
`backend/src/data/embeddings.py`'s `HashingEmbedder`):

```bash
uv run python -c "from data.seed import seed_all; print(seed_all())"
```

## Documentation

- `docs/SPEC.md` — intent: requirements, constraints, success criteria.
- `docs/DESIGN.md` — contracts: data models, agent graph, HelpdeskPort,
  portal API, metric definitions.
- `docs/tickets.json` / `docs/TASKS.md` — the task graph this repo was
  built from (JSON is authoritative; the `.md` is its human-readable
  mirror).
- `docs/zendesk-runbook.md` — the human steps to stand up a real Zendesk
  trial: OAuth app, AI agent user, trigger, webhook + signing secret,
  `cloudflared` tunnel for local dev.
- `docs/deploy.md` — how to put this on a DigitalOcean droplet: sizing,
  install steps, getting the repo there, supplying `.env` safely, running
  the stack, pointing the Zendesk webhook at it, running
  `scripts/verify_deploy.sh` against it.
- `docs/eval-report/` — the escalation precision/recall report generated
  from `evals/labeled_set.yaml` (`uv run python -m evals.report`). **Draft**
  — see Status below.
- `docs/architecture.md`, `docs/grounding.md`, `docs/portability.md`,
  `docs/demo-script.md` — system architecture (+ diagram), grounding
  design, the HelpdeskPort portability story, and the demo shot list,
  each written directly to the acceptance criteria in `docs/tickets.json`'s
  `T-11` entry.

## Status — what's actually verified here, and what isn't

Said plainly, not overstated:

**Live-verified, on this machine, as part of building this:**
- `uv run pytest` (222 tests), `uv run pytest -m contract -q` (both
  `HelpdeskPort` adapters), `uv run ruff check .`, `uv run mypy backend` —
  all green.
- `cd portal && npm run build` (typechecks + builds) and `npm test`
  (5 component tests) — both green.
- The production stack (`deploy/docker-compose.yml`): built, brought up,
  and torn down locally with `bash scripts/verify_deploy.sh`, which
  asserts `GET /health` is 200, the portal serves its built index,
  `GET /api/metrics` is 401 with no token and 200 with the right
  `X-Portal-Token` — all through the portal's nginx reverse proxy, the
  same single entry point a deployed droplet would expose. The backend
  image bootstraps its own schema and seeds 30 fixture cases / 44 KB
  chunks on first start (`deploy/backend/bootstrap.py`).
- The dev Postgres container (`othram-db`) was left completely undisturbed
  by all of the above — confirmed by checking its status before and after.

**Not verified — stated honestly rather than glossed over:**
- **No droplet exists.** `docs/deploy.md` is the procedure to create one;
  `scripts/verify_deploy.sh` supports pointing at one (`DEPLOY_HOST`) once
  it does, but that path has not been exercised here.
- **Zendesk live e2e is unrun.** `ZENDESK_OAUTH_TOKEN`,
  `ZENDESK_WEBHOOK_SIGNING_SECRET`, `ZENDESK_AI_USER_ID`, and
  `ZENDESK_OAUTH_CLIENT_ID` are all empty in this environment — no trial
  account has been signed up for (`docs/zendesk-runbook.md` is a human
  step, not yet done). The app is designed to degrade honestly without
  them (every read of a Zendesk env var happens at request time, never at
  import/startup — see `backend/src/ingress/__init__.py` and
  `backend/src/helpdesk/zendesk_adapter.py`), and that degrade behavior
  itself IS verified (the production image starts and serves `/health`
  with zero Zendesk credentials set), but the real trigger→webhook→
  reply round trip against a live Zendesk instance is not.
- **The agent graph's LLM-backed nodes are not exercised in CI.** Every
  graph/grounding test runs against a fake `LLMClient`
  (`backend/tests/graph/fakes.py`), so a green suite says nothing about
  real model behaviour. Real calls go to the Anthropic Messages API
  (`backend/src/agent/llm.py`'s `AnthropicLLMClient`, model pinned in
  `backend/src/agent/config.py`) and need `ANTHROPIC_API_KEY` set.

## Portability: the HelpdeskPort boundary

Every write the agent makes to a helpdesk — reading a ticket and its
conversation, posting a public reply or an internal note, tagging, changing
status, assigning a group — goes through one Python `Protocol`,
`HelpdeskPort` (`backend/src/helpdesk/port.py`), never through a concrete
provider class. Two implementations of it exist:

- **`ZendeskAdapter`** (`backend/src/helpdesk/zendesk_adapter.py`) — the
  real integration: OAuth 2.0 bearer auth, Zendesk's REST API, 429/5xx
  backoff honoring `Retry-After`, the `ai-processed` loop-guard tag folded
  into every write.
- **`EmailAdapter`** (`backend/src/helpdesk/email_adapter.py`) — a stub
  built on an in-memory fake mail transport. No HTTP, no Zendesk ticket
  shape, nothing in common with `ZendeskAdapter`'s transport at all.

Both pass the **identical, unmodified** parametrized contract suite
(`backend/tests/contract/test_port_contract.py`, run via
`pytest -m contract`) — the same test bodies, run once per adapter via a
`pytest` fixture, asserting only against `HelpdeskPort`'s Protocol surface.
That's the differentiation artifact this project is judged on (SPEC R14):
proof that nothing Zendesk-specific leaked into the port abstraction, by
demonstrating a second, structurally unrelated implementation satisfies it
without a single special case.

### What the EmailAdapter stub deliberately does NOT implement

`EmailAdapter` models the email domain honestly — a ticket is a mail
thread, a public reply is an outbound email to the requester, an internal
note is a non-public annotation never emailed to anyone, and tags/status/
group are local thread metadata with no wire representation in plain
SMTP/IMAP — but it stays a stub on purpose (T-3 non-goal: "no real
SMTP/IMAP"). It exists solely to prove the port contract, not to be a
usable channel. A production email channel would need to add, none of
which this stub implements:

- **Inbound intake** — IMAP/POP polling or an inbound-mail webhook
  (e.g. a provider like Postmark/SendGrid parsing inbound mail and posting
  it to us) to learn about new messages at all. This stub's equivalent is
  a same-process `seed_ticket`/`seed_comment` call from a test.
- **Thread correlation** — real threading via the `Message-ID`,
  `In-Reply-To`, and `References` headers, so a customer's reply lands on
  the right existing ticket instead of opening a new one. This stub
  threads by an in-memory ticket id it invents itself.
- **MIME parsing and quoted-reply stripping** — decoding multipart
  messages (HTML vs. plain-text parts, attachments) and stripping the
  quoted history a mail client appends to every reply, so only the new
  content is treated as the customer's message.
- **Bounce and auto-responder handling** — detecting NDRs, out-of-office
  auto-replies, and mailer-daemon messages so they don't get treated as
  genuine customer replies (and, for this system specifically, don't loop
  back through the agent the way an unguarded auto-reply chain would).
- **Per-recipient delivery status** — tracking whether a sent message
  actually reached the requester's inbox (bounced, deferred, delivered),
  which a real SMTP relay or provider webhook reports asynchronously,
  well after the "send" call returns.

`InMemoryEmailTransport.send()` only records that a send was requested —
no socket is opened, nothing crosses the network, and there is no delivery
status to report because there is no delivery.
