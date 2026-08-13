# Othram AI Support Agent

An autonomous AI support agent (Gauntlet challenger project, PRD_02 /
Othram) that handles inbound tickets for a fictionalized forensic-genomics
lab: answers routine inquiries grounded in a knowledge base and a live
case-management system, and escalates to a human only when necessary. A
React review portal (draft feed + approval gate) ships alongside it.

Full documentation — architecture, grounding design, escalation
methodology, deployment — lands under `docs/` as later tickets complete
(see `docs/SPEC.md`, `docs/DESIGN.md`, and `docs/tickets.json` for the
build plan this repo is executing against). This README stays intentionally
brief; T-11 owns the complete write-up.

## Quickstart

```bash
cp .env.example .env          # fill in as each ticket's docs/*-runbook.md requires
docker compose up -d db       # Postgres 16 + pgvector
uv sync
uv run pytest                 # full suite
uv run pytest -m contract -q  # HelpdeskPort contract suite only
uv run ruff check .
uv run mypy backend
```

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
