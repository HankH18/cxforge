# cxforge — AI support agent for Zendesk

An autonomous support agent for a fictionalized forensic-genomics lab ("Othram"). It reads inbound Zendesk tickets, answers routine questions from a knowledge base and case data using Claude, and escalates to a human (internal note + group assignment) for billing disputes, requests for a human, frustration or complexity. A React portal shows every draft and has an approval gate.

- **Recorded live result:** on 2026-08-17 a real requester comment on a live Zendesk ticket ran the whole loop on the deployed stack (webhook → Cloudflare Tunnel → Redis → worker → Claude → reply posted) in 11.5 s. See `docs/BUILD-PLAN.md` §10.6.
- **Eval:** 30/30 route accuracy with `claude-opus-5` on the branch-route tickets of a 51-ticket labeled set (`evals/route-accuracy/report.md`).
- **Run it locally:** follow the Quickstart below (Postgres in Docker, `uv sync`, backend on :8000, portal on <http://localhost:5173>). Seeding and the default test run need no API key.
- **Stack:** FastAPI, Postgres + pgvector, Redis/arq, Anthropic Claude, Langfuse, promptfoo, React, Docker Compose, Cloudflare Tunnel.
- **Read next:** architecture `docs/architecture.md` · current state `docs/STATE.md`

Built from a fixed spec (`docs/SPEC.md`), design (`docs/DESIGN.md`) and task graph (`docs/tickets.json`).

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

# 3. Full test suite
uv run pytest
uv run pytest -m contract -q    # HelpdeskPort contract suite only — both adapters
uv run ruff check .
uv run mypy backend

# 4. Run the backend app (dev mode — for the production image, see
#    deploy/Dockerfile.backend and docs/deploy.md instead)
PYTHONPATH=backend/src uv run uvicorn main:app --reload --port 8000
# -> GET http://localhost:8000/health  ==>  {"status": "ok"}

# 4b. Run the queue worker (needs Redis; deploy/docker-compose.yml runs redis, worker and cloudflared as services)
PYTHONPATH=backend/src uv run arq worker.main.WorkerSettings

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

- `docs/STATE.md` — verified current state (updated 2026-08-17); supersedes
  `docs/HANDOFF.md`.
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
  from `evals/labeled_set.yaml` (`uv run python -m evals.report`).
  Hard-trigger recall of 1.0 covers 10 of 16 hard-trigger tickets; 6 are not
  driven by the eval (`docs/STATE.md` §4.2).
- `docs/architecture.md`, `docs/grounding.md`, `docs/portability.md`,
  `docs/demo-script.md` — system architecture (+ diagram), grounding
  design, the HelpdeskPort portability story, and the demo shot list,
  each written directly to the acceptance criteria in `docs/tickets.json`'s
  `T-11` entry.

## Status

The deployment described here is not running today; these are records of what was measured while it ran.

**Verified on the deployed system (2026-08-17; `docs/STATE.md` §1, `docs/BUILD-PLAN.md` §10.6):**
- A real requester comment on live Zendesk ticket 3 ran the whole loop on the droplet: Zendesk trigger → Cloudflare Tunnel → ingress → Redis → arq worker → 2 Claude calls → 3 Zendesk writes. Run time 11.477 s, route `case_status`, confidence 0.98, outcome `auto_sent`. The reply was posted publicly on the ticket.
- `/api/metrics` through the tunnel reported `sample_count: 3`, p50 15.0 s, p95 15.3 s.
- Route classification: 30/30 on the branch-route tickets of a 51-ticket labeled set with `claude-opus-5` (`evals/route-accuracy/report.md`).
- CI run 32010188872: 551 passed / 1 skipped (backend) and 36 Vitest tests (portal).

**Open:**
- The 20–30 ticket scenario run (ADR-015) has not been done; live latency rests on 3 samples.
- The Zendesk-originated trigger path was not re-measured after the 2026-08-17 tunnel recovery (`docs/STATE.md` §3.4).
- Hard-trigger escalation recall of 1.0 covers 10 of 16 hard-trigger tickets; the other 6 are not driven by the eval (`docs/STATE.md` §4.2).
- Graph and grounding tests in CI run against a fake `LLMClient`; real model behaviour is measured only by the evals above.

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
