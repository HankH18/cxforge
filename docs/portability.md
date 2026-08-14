# Portability: the HelpdeskPort boundary

> **R14** (`docs/SPEC.md`): "An `EmailAdapter` stub (fake transport) SHALL
> pass the identical `HelpdeskPort` contract test suite as
> `ZendeskAdapter`."

## The Protocol

`backend/src/helpdesk/port.py`:

```python
class HelpdeskPort(Protocol):
    def fetch_ticket(self, ticket_id: str) -> Ticket: ...
    def fetch_conversation(self, ticket_id: str) -> list[Message]: ...
    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef: ...
    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef: ...
    def add_tags(self, ticket_id: str, tags: list[str]) -> None: ...
    def set_status(self, ticket_id: str, status: TicketStatus) -> None: ...
    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None: ...
```

Every consumer in this codebase — the agent graph (`agent/nodes.py`), the
escalation `act` step, the portal's approve flow (`portal/service.py`) —
depends on this Protocol, never on a concrete adapter class.

## Normalized models

`backend/src/helpdesk/models.py`:

- `Ticket(id, subject, requester_email, status, tags, created_at)`
- `Message(id, author_kind: Literal["customer","agent","ai"], text,
  public, created_at)`
- `MessageRef(ticket_id, message_id, public)` — just enough for a caller
  to confirm which ticket got which kind of message, without exposing any
  provider-specific comment shape.
- `EscalationGroup(group_id, name)` — `group_id` is the provider's own
  identifier, carried as a string so the type itself stays
  provider-agnostic.
- `TicketStatus = Literal["new", "open", "pending", "solved"]`

Provider quirks never leak into these types by design — a field only one
provider can populate belongs in that adapter's own internals, not here.

## What lives inside `ZendeskAdapter` only

`backend/src/helpdesk/zendesk_adapter.py` — everything Zendesk-specific
in this codebase is here and nowhere else:

- **OAuth 2.0 bearer auth only.** `__init__` raises
  `HelpdeskConfigError` if `ZENDESK_SUBDOMAIN`/`ZENDESK_OAUTH_TOKEN` are
  missing; there is no API-token code path anywhere in this file (Zendesk
  API tokens are on a staged removal schedule per `docs/DESIGN.md`'s
  rationale, and are forbidden by SPEC).
- **One comment per PUT.** `post_public_reply` and `post_internal_note`
  are necessarily two separate `_update_ticket` calls — Zendesk's API
  accepts only one comment per ticket update, so a public reply and an
  internal note can never be combined into a single request. The contract
  suite (below) has a dedicated test for this
  (`test_public_reply_and_internal_note_are_separate_and_correctly_scoped`).
- **The `ai-processed` loop-guard tag funnel.** Every write funnels
  through the single `_update_ticket` method, which unconditionally folds
  `ai-processed` into `additional_tags` (the *additive* field — Zendesk's
  plain `tags` field on an update *replaces* the set, which would silently
  wipe the loop guard if used instead). There is no way to construct a
  write on this adapter that skips the tag.
- **Retry-After / backoff.** `_request` uses `tenacity.Retrying`: a 429
  response raises `RateLimited` and waits exactly the response's
  `Retry-After` header (parsed by `_parse_retry_after`, defaulting to 1.0s
  if the header is missing or unparseable); a 5xx raises
  `ServerUnavailable` and backs off exponentially, capped at 30 seconds.
  Other 4xx responses are not retried and surface immediately as
  `HelpdeskAPIError`. `max_attempts` and `sleep` are both injectable
  (`backend/tests/contract/test_zendesk_adapter.py` uses a fake `sleep` so
  these tests run instantly, not in wall-clock retry time).
- **Author-identity mapping.** `_author_kind` maps a raw Zendesk numeric
  user id to the normalized `AuthorKind` literal (comparing against
  `ZENDESK_AI_USER_ID` for `"ai"`, and the Zendesk user's `role` field for
  `"customer"` vs `"agent"`) — callers never see a raw provider user id.

## R14 evidence: the identical, unmodified contract suite

`backend/tests/contract/test_port_contract.py` is written **once**
against the `HelpdeskPort` Protocol and the `AdapterHarness` fixture
(`backend/tests/contract/conftest.py`) — it imports no concrete adapter
and reaches into no adapter's transport. Twelve test cases (4 single tests
+ `test_every_write_appends_ai_processed_tag` parametrized over 5 write
operations + 3 more single tests), each run once per adapter via
`pytest`'s own parametrization (`ADAPTER_FACTORIES = {"zendesk":
make_zendesk_harness, "email": make_email_harness}`) — **12 shared tests
× 2 adapters = 24 test executions**, all passing today (part of the
222-test suite; run in isolation via `pytest -m contract`).

The strongest form of this evidence isn't "the suite passes for both
adapters" (a suite could pass trivially if it were quietly special-cased
per adapter) — it's that **the suite file itself never changed** when the
second adapter was added. Checking this repo's own git history:

```
c544008  T-2: HelpdeskPort, ZendeskAdapter, contract suite   (creates test_port_contract.py)
573a69a  T-3: EmailAdapter stub passes the contract suite    (does NOT touch test_port_contract.py)
```

Adding `EmailAdapter` to the parametrization was a one-line addition to
`conftest.py`'s `ADAPTER_FACTORIES` dict (plus the matching import) — the
177-line test file that defines what "passes the contract" means is
byte-for-byte identical before and after. That is the actual R14 proof:
nothing Zendesk-specific leaked into the port abstraction, demonstrated
by a structurally unrelated second implementation satisfying the exact
same unmodified assertions.

## `EmailAdapter`: a contract-proving stub, not a real channel

`backend/src/helpdesk/email_adapter.py` implements `HelpdeskPort` over
`InMemoryEmailTransport` — a fake mail transport that records sends in a
Python list. **No socket is ever opened, no network call is ever made.**
Domain modeling is honest within that constraint: a ticket is a mail
thread; a public reply is an outbound email to the requester; an internal
note is a non-public annotation kept in the adapter's local thread store
(plain email has no "private comment" concept, so a real production
adapter would need its own side channel for this too — this stub's local
store stands in for that); tags/status/group assignment are local thread
metadata with no SMTP/IMAP wire representation. It mirrors
`ZendeskAdapter`'s loop-guard discipline with its own single funnel point
(`_touch`), and its own test/dev seeding surface (`seed_ticket`,
`seed_comment`) stands in for what a production adapter would learn from
real inbound mail — that surface is not part of `HelpdeskPort` and is
used only by the contract suite's harness.

**What a real production email channel would still need to add, none of
which this stub implements:**

- **Inbound intake** — IMAP/POP polling, or an inbound-mail webhook (a
  provider like Postmark/SendGrid parsing inbound mail and posting it to
  this service) to learn about new messages at all. The stub's equivalent
  is a same-process `seed_ticket`/`seed_comment` call from a test.
- **Thread correlation** — real threading via the `Message-ID`,
  `In-Reply-To`, and `References` headers, so a customer's reply lands on
  the existing ticket instead of opening a new one. The stub threads by
  an in-memory id it invents itself.
- **MIME parsing and quoted-reply stripping** — decoding multipart
  messages and stripping the quoted history a mail client appends to
  every reply, so only new content is treated as the customer's message.
- **Bounce and auto-responder handling** — detecting NDRs, out-of-office
  auto-replies, and mailer-daemon messages so they aren't treated as
  genuine customer replies (and, specifically for this system, don't loop
  back through the agent the way an unguarded auto-reply chain would).
- **Per-recipient delivery status** — tracking whether a sent message
  actually reached the requester's inbox, which a real SMTP relay or
  provider webhook reports asynchronously, well after the "send" call
  returns.

## Future adapters: Gorgias, Intercom, Front

None of the three below exist in this codebase yet — this section is
forward-looking design commentary on what the `HelpdeskPort` boundary
would require of them, not a claim that any integration work has started.
Each would live entirely in its own file (`gorgias_adapter.py`,
`intercom_adapter.py`, `front_adapter.py`), touching nothing in
`helpdesk/port.py` or `helpdesk/models.py`, exactly as `ZendeskAdapter`
and `EmailAdapter` do today.

- **Gorgias** — REST API auth (OAuth 2.0 or a scoped API key, per
  whichever Gorgias currently supports and this project's OAuth-only
  constraint would require checking against); mapping Gorgias tickets and
  their "internal notes" concept (Gorgias distinguishes public replies
  from internal notes natively, unlike raw email) onto `Ticket`/`Message`;
  a tags API for `add_tags`; a rate-limit/backoff strategy analogous to
  `ZendeskAdapter`'s `Retry-After` handling, tuned to whatever Gorgias's
  actual rate-limit headers are; and a loop-guard equivalent to
  `ai-processed` — some tag or status funnel this project's webhook
  trigger (or Gorgias's own rule/automation feature) can key a
  nullifying condition off of.
- **Intercom** — the Conversations API, which (like Gorgias) has a native
  distinction between a public reply and an internal note, so
  `post_public_reply`/`post_internal_note` map cleanly without the
  two-separate-writes complexity Zendesk's one-comment-per-PUT limit
  imposes; admin/team assignment mapping onto `assign_group`; Intercom's
  own webhook signature scheme (HMAC-based, analogous in shape to
  `ingress/signature.py`'s Zendesk verification, but not identical) for
  an equivalent ingress endpoint; and its own loop-guard equivalent, since
  Intercom's own bots/workflows can just as easily re-trigger on the
  agent's own writes without one.
- **Front** — the Conversations/Comments API, which also distinguishes
  drafts/comments (internal) from actual sent replies; teammate/inbox-based
  routing as the natural mapping for `assign_group` (Front organizes
  around shared inboxes and teammates rather than a single flat group
  concept, so this mapping needs more thought than Zendesk's group_id);
  Front's own webhook signature verification; and, again, a loop-guard
  equivalent scoped to however Front's own rules/automations can be
  configured to recognize the agent's own writes.

For all three, the actual amount of new code needed is bounded by what
`HelpdeskPort` requires — seven methods, mapped onto whatever that
provider's API offers for the same seven concepts — which is the entire
point of the boundary this document describes.

See `docs/architecture.md` for how `HelpdeskPort` fits into the agent
graph's `act` step, and `docs/demo-script.md` for what a live Zendesk
demonstration of `ZendeskAdapter` still requires (credentials that don't
exist in this environment yet).
