"""In-memory Zendesk simulator backing the contract suite's Zendesk harness.

Not a test module itself (no ``test_`` prefix — pytest never collects it).
Registers respx routes over a real ``httpx.Client``, so ``ZendeskAdapter``
under test makes genuine HTTP requests (auth header, JSON bodies, retry
loop) against handlers here rather than canned per-call responses. Write
handlers mutate the same store ``fetch_ticket``/``fetch_conversation`` read
from, so a contract test can verify a write purely by reading it back
through the Protocol — exactly the way a real Zendesk trial would behave.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import httpx
import respx

from helpdesk.models import AuthorKind
from helpdesk.port import HelpdeskPort
from helpdesk.zendesk_adapter import ZendeskAdapter

SUBDOMAIN = "othram-test"
BASE_URL = f"https://{SUBDOMAIN}.zendesk.com/api/v2"
OAUTH_TOKEN = "test-oauth-bearer-token"  # fixture value, not a real credential

AI_USER_ID = "9001"
CUSTOMER_USER_ID = "100"
AGENT_USER_ID = "200"
DEFAULT_REQUESTER_EMAIL = "requester@example.com"

_AUTHOR_TO_USER_ID = {"customer": CUSTOMER_USER_ID, "agent": AGENT_USER_ID, "ai": AI_USER_ID}
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Seeded:
    """A ticket pre-seeded into an adapter's backing store."""

    ticket_id: str
    requester_email: str


class FakeZendesk:
    """Owns the ticket/comment/user store and the respx route handlers over it."""

    def __init__(self, router: respx.Router) -> None:
        # Kept for test inspection (e.g. asserting the Authorization header
        # of a captured request) — respx records every matched call here
        # regardless of mock type, so this needs no extra bookkeeping.
        self.router = router
        self.tickets: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.users: dict[str, dict[str, Any]] = {
            CUSTOMER_USER_ID: {
                "id": int(CUSTOMER_USER_ID),
                "email": DEFAULT_REQUESTER_EMAIL,
                "role": "end-user",
            },
            AGENT_USER_ID: {"id": int(AGENT_USER_ID), "role": "agent"},
            # Zendesk has no separate "AI" role — the dedicated AI agent user
            # is a plain agent. ai_user_id (checked first in the adapter) is
            # what actually distinguishes it, not this role field.
            AI_USER_ID: {"id": int(AI_USER_ID), "role": "agent"},
        }
        self._ticket_ids = count(1)
        self._comment_ids = count(1000)
        self._audit_ids = count(5000)
        self._auto_clock = count(0)
        # One Zendesk user per distinct requester email. Previously this
        # simulator overwrote CUSTOMER_USER_ID's email on every seed, so the
        # whole store had exactly one requester — which is fine until a test
        # needs to prove `fetch_requester_history` does not return SOMEBODY
        # ELSE's tickets, the single most important thing that method must
        # get right. The default email keeps CUSTOMER_USER_ID so every
        # existing test's author-kind mapping is unchanged.
        self._requester_ids: dict[str, str] = {DEFAULT_REQUESTER_EMAIL: CUSTOMER_USER_ID}
        self._extra_user_ids = count(300)
        self.put_calls: dict[str, int] = {}
        self._queued: dict[str, list[httpx.Response]] = {}

        router.route(method="GET", path__regex=r"^/tickets/(?P<ticket_id>\d+)\.json$").mock(
            side_effect=self._get_ticket
        )
        router.route(
            method="GET", path__regex=r"^/tickets/(?P<ticket_id>\d+)/comments\.json$"
        ).mock(side_effect=self._get_comments)
        router.route(method="PUT", path__regex=r"^/tickets/(?P<ticket_id>\d+)\.json$").mock(
            side_effect=self._put_ticket
        )
        router.route(method="GET", path__regex=r"^/search\.json$").mock(
            side_effect=self._search
        )

    # -- seeding / inspection API used by the harness and adapter-specific tests --

    def seed_ticket(
        self,
        *,
        requester_email: str = DEFAULT_REQUESTER_EMAIL,
        subject: str = "Where is my case?",
        status: str = "open",
        tags: list[str] | None = None,
    ) -> str:
        ticket_id = str(next(self._ticket_ids))
        requester_id = self._requester_id_for(requester_email)
        self.tickets[ticket_id] = {
            "id": int(ticket_id),
            "subject": subject,
            "requester_id": int(requester_id),
            "status": status,
            "tags": list(tags or []),
            "created_at": self._next_timestamp(),
            "group_id": None,
        }
        self.comments[ticket_id] = []
        return ticket_id

    def _requester_id_for(self, email: str) -> str:
        existing = self._requester_ids.get(email)
        if existing is not None:
            return existing
        user_id = str(next(self._extra_user_ids))
        self._requester_ids[email] = user_id
        self.users[user_id] = {"id": int(user_id), "email": email, "role": "end-user"}
        return user_id

    def seed_comment(
        self,
        ticket_id: str,
        *,
        author: AuthorKind,
        text: str,
        public: bool = True,
        created_at: str | None = None,
    ) -> None:
        comment_id = next(self._comment_ids)
        self.comments[ticket_id].append(
            {
                "id": comment_id,
                "author_id": int(_AUTHOR_TO_USER_ID[author]),
                "body": text,
                "html_body": text,
                "public": public,
                "created_at": created_at or self._next_timestamp(),
            }
        )

    def queue_response(self, ticket_id: str, response: httpx.Response) -> None:
        """Make the *next* PUT to this ticket return ``response`` verbatim
        instead of being applied to the store — used to simulate 429/5xx/4xx
        straight from "Zendesk"."""
        self._queued.setdefault(ticket_id, []).append(response)

    def group_id_for(self, ticket_id: str) -> str | None:
        group_id = self.tickets[ticket_id]["group_id"]
        return None if group_id is None else str(group_id)

    def _next_timestamp(self) -> str:
        return (_EPOCH + timedelta(seconds=next(self._auto_clock))).isoformat()

    # -- route handlers -----------------------------------------------------

    def _get_ticket(self, request: httpx.Request, ticket_id: str) -> httpx.Response:
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            return httpx.Response(404, json={"error": "RecordNotFound"})
        payload: dict[str, Any] = {"ticket": ticket}
        if request.url.params.get("include") == "users":
            payload["users"] = list(self.users.values())
        return httpx.Response(200, json=payload)

    def _get_comments(self, request: httpx.Request, ticket_id: str) -> httpx.Response:
        payload: dict[str, Any] = {"comments": self.comments.get(ticket_id, [])}
        if request.url.params.get("include") == "users":
            payload["users"] = list(self.users.values())
        return httpx.Response(200, json=payload)

    def _search(self, request: httpx.Request) -> httpx.Response:
        """``GET /search.json`` — enough of the Search API to be a real test
        of ``fetch_requester_history``'s request construction.

        Parses the ``query`` string rather than accepting any query and
        returning everything: an adapter that forgot ``type:ticket``, or
        that put the requester's *id* where Zendesk wants an email, or that
        tried to express sorting inside the query string instead of via
        ``sort_by``/``sort_order``, has to fail here. A handler that ignored
        the query would make the contract test assert nothing about the
        request at all.
        """
        params = request.url.params
        query = params.get("query", "")
        terms = dict(
            term.split(":", 1) for term in query.split() if ":" in term
        )
        if terms.get("type") != "ticket":
            return httpx.Response(
                422, json={"error": "InvalidSearch", "description": f"unsupported: {query!r}"}
            )
        requester = terms.get("requester")
        if requester is None:
            return httpx.Response(
                422, json={"error": "InvalidSearch", "description": "no requester term"}
            )
        requester_id = self._requester_ids.get(requester)

        results = [
            ticket
            for ticket in self.tickets.values()
            if requester_id is not None and ticket["requester_id"] == int(requester_id)
        ]
        if params.get("sort_by") == "created_at":
            results.sort(
                key=lambda t: t["created_at"], reverse=params.get("sort_order") == "desc"
            )
        per_page = int(params.get("per_page", "100"))
        return httpx.Response(
            200, json={"results": results[:per_page], "count": len(results)}
        )

    def _put_ticket(self, request: httpx.Request, ticket_id: str) -> httpx.Response:
        self.put_calls[ticket_id] = self.put_calls.get(ticket_id, 0) + 1
        queued = self._queued.get(ticket_id)
        if queued:
            return queued.pop(0)

        ticket = self.tickets[ticket_id]
        patch = json.loads(request.content)["ticket"]
        comment_event: dict[str, Any] | None = None

        if "comment" in patch:
            comment = patch["comment"]
            comment_id = next(self._comment_ids)
            is_public = bool(comment.get("public", True))
            self.comments[ticket_id].append(
                {
                    "id": comment_id,
                    # Every write in production reaches Zendesk via the AI
                    # agent user's own OAuth token, so every comment this
                    # simulator creates is authored by it.
                    "author_id": int(AI_USER_ID),
                    "body": comment.get("body", comment.get("html_body", "")),
                    "html_body": comment.get("html_body", comment.get("body", "")),
                    "public": is_public,
                    "created_at": self._next_timestamp(),
                }
            )
            comment_event = {"type": "Comment", "id": comment_id, "public": is_public}

        # Real Zendesk: `tags` REPLACES the ticket's entire tag set, while
        # `additional_tags` is purely additive. Applying `tags` first (if
        # present) and then folding `additional_tags` on top mirrors what a
        # single PUT carrying both would do against the real API, and is
        # what makes this fake capable of reproducing the clobbering a
        # `tags` write would inflict (including wiping the ai-processed
        # loop-guard tag) rather than silently ignoring the field.
        if "tags" in patch:
            ticket["tags"] = list(patch["tags"])
        if "additional_tags" in patch:
            existing = ticket["tags"]
            for tag in patch["additional_tags"]:
                if tag not in existing:
                    existing.append(tag)
        if "status" in patch:
            ticket["status"] = patch["status"]
        if "group_id" in patch:
            ticket["group_id"] = patch["group_id"]

        events = [comment_event] if comment_event else []
        audit = {"id": next(self._audit_ids), "events": events}
        return httpx.Response(200, json={"ticket": ticket, "audit": audit})


@dataclass
class ZendeskHarness:
    """AdapterHarness for ZendeskAdapter, plus the extra transport-level
    inspection (``fake``, ``sleeps``) that only the Zendesk-specific tests
    in test_zendesk_adapter.py need — the generic parametrized suite only
    ever touches ``port``, ``seed_ticket``, ``seed_comment``, and
    ``group_id_for``."""

    # Typed as the Protocol, not the concrete class: AdapterHarness (which
    # this structurally satisfies) declares `port: HelpdeskPort`, and a
    # mutable dataclass field must match a Protocol attribute's type
    # exactly, not just be a compatible subtype of it. Zendesk-specific
    # tests that need adapter internals use `.fake`/`.sleeps` instead.
    port: HelpdeskPort
    fake: FakeZendesk
    sleeps: list[float] = field(default_factory=list)

    def seed_ticket(
        self,
        *,
        tags: list[str] | None = None,
        subject: str = "Where is my case?",
        requester_email: str = DEFAULT_REQUESTER_EMAIL,
    ) -> Seeded:
        ticket_id = self.fake.seed_ticket(
            tags=tags, subject=subject, requester_email=requester_email
        )
        return Seeded(ticket_id=ticket_id, requester_email=requester_email)

    def seed_comment(
        self,
        ticket_id: str,
        *,
        author: AuthorKind,
        text: str,
        public: bool = True,
        created_at: str | None = None,
    ) -> None:
        self.fake.seed_comment(
            ticket_id, author=author, text=text, public=public, created_at=created_at
        )

    def group_id_for(self, ticket_id: str) -> str | None:
        return self.fake.group_id_for(ticket_id)


def make_zendesk_harness() -> Iterator[ZendeskHarness]:
    """Factory registered in conftest.py's ``ADAPTER_FACTORIES``.

    A generator so the ``respx.mock`` context manager's teardown (un-patching
    httpx) runs when the owning pytest fixture is torn down, not before.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        fake = FakeZendesk(router)
        sleeps: list[float] = []
        adapter = ZendeskAdapter(
            subdomain=SUBDOMAIN,
            oauth_token=OAUTH_TOKEN,
            ai_user_id=AI_USER_ID,
            sleep=sleeps.append,
        )
        yield ZendeskHarness(port=adapter, fake=fake, sleeps=sleeps)
