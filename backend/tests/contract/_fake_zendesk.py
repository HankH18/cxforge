"""In-memory Zendesk simulator backing the contract suite's Zendesk harness.

Not a test module itself (no ``test_`` prefix — pytest never collects it).
Registers respx routes over a real ``httpx.Client``, so ``ZendeskAdapter``
under test makes genuine HTTP requests (auth header, JSON bodies, retry
loop) against handlers here rather than canned per-call responses. Write
handlers mutate the same store ``fetch_ticket``/``fetch_conversation`` read
from, so a contract test can verify a write purely by reading it back
through the Protocol — exactly the way a real Zendesk trial would behave.

**This simulator lied for the project's whole life, and that is why a
loop guard was down in production.** ``_put_ticket`` implemented
``additional_tags`` as an additive tag write, because the adapter assumed it
was one. Real Zendesk *discards* that field on a single-ticket update. So
``test_every_write_appends_ai_processed_tag`` and ``test_add_tags_is_additive``
were green against a fake that granted the assumption, while a complete
successful production run left no ``ai-processed`` tag on the ticket at all.
Every tag rule below is now a measurement against the live account
(2026-08-17), recorded next to the handler that implements it — see
``ZendeskAdapter._merge_tags`` for the raw table. When this fake and Zendesk
disagree, the fake is wrong; check it against the account before "fixing" an
adapter to match it.
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
        # PUTs to `/tickets/{id}.json` only — the tags sub-resource is counted
        # separately in `tag_put_calls` so the retry tests, which assert exact
        # ticket-update counts, are unaffected by the loop-guard tag write.
        self.put_calls: dict[str, int] = {}
        self.tag_put_calls: dict[str, int] = {}
        self._queued: dict[str, list[httpx.Response]] = {}
        self._queued_tags: dict[str, list[httpx.Response]] = {}
        # Who this fake's OAuth token acts as, i.e. what `GET /users/me.json`
        # answers and who authors every comment a write creates. Defaults to
        # the dedicated AI user, which is the CORRECT production state (config
        # agrees with the token). A test flips it to reproduce the 2026-08-17
        # defect, where the token acted as the owner's admin account instead.
        self.authenticated_user_id: str = AI_USER_ID

        router.route(method="GET", path__regex=r"^/tickets/(?P<ticket_id>\d+)\.json$").mock(
            side_effect=self._get_ticket
        )
        router.route(
            method="GET", path__regex=r"^/tickets/(?P<ticket_id>\d+)/comments\.json$"
        ).mock(side_effect=self._get_comments)
        router.route(method="PUT", path__regex=r"^/tickets/(?P<ticket_id>\d+)\.json$").mock(
            side_effect=self._put_ticket
        )
        # The tags sub-resource. Registered AFTER the ticket-update route above
        # but matched by its own regex, so `/tickets/1/tags.json` can never be
        # served by `_put_ticket` (whose pattern anchors on `.json` immediately
        # after the id).
        router.route(method="PUT", path__regex=r"^/tickets/(?P<ticket_id>\d+)/tags\.json$").mock(
            side_effect=self._put_tags
        )
        router.route(method="POST", path__regex=r"^/tickets/(?P<ticket_id>\d+)/tags\.json$").mock(
            side_effect=self._post_tags
        )
        router.route(method="GET", path__regex=r"^/search\.json$").mock(
            side_effect=self._search
        )
        router.route(method="GET", path__regex=r"^/users/me\.json$").mock(
            side_effect=self._get_me
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

    def queue_tag_response(self, ticket_id: str, response: httpx.Response) -> None:
        """Same, for the next ``PUT /tickets/{id}/tags.json`` — used to prove a
        failed loop-guard tag write ABORTS the write it was guarding rather
        than letting an untagged public reply out."""
        self._queued_tags.setdefault(ticket_id, []).append(response)

    def group_id_for(self, ticket_id: str) -> str | None:
        group_id = self.tickets[ticket_id]["group_id"]
        return None if group_id is None else str(group_id)

    def _next_timestamp(self) -> str:
        return (_EPOCH + timedelta(seconds=next(self._auto_clock))).isoformat()

    # -- route handlers -----------------------------------------------------

    def _get_me(self, request: httpx.Request) -> httpx.Response:
        """``GET /users/me.json`` — the identity the token acts as.

        The single fact nothing in this project ever checked, and the reason
        ``ZENDESK_AI_USER_ID`` could name a user that appears in no event the
        system will ever see.
        """
        user = self.users.get(
            self.authenticated_user_id,
            {"id": int(self.authenticated_user_id), "role": "admin"},
        )
        return httpx.Response(200, json={"user": user})

    def _put_tags(self, request: httpx.Request, ticket_id: str) -> httpx.Response:
        """``PUT /tickets/{id}/tags.json`` — ADDITIVE. Measured 2026-08-17:
        against tags ``['replace-probe']``, ``{"tags": ["ai-processed"]}``
        yielded ``['ai-processed', 'replace-probe']``. Returns the merged set.
        """
        self.tag_put_calls[ticket_id] = self.tag_put_calls.get(ticket_id, 0) + 1
        queued = self._queued_tags.get(ticket_id)
        if queued:
            return queued.pop(0)
        existing = self.tickets[ticket_id]["tags"]
        for tag in json.loads(request.content)["tags"]:
            if tag not in existing:
                existing.append(tag)
        return httpx.Response(200, json={"tags": list(existing)})

    def _post_tags(self, request: httpx.Request, ticket_id: str) -> httpx.Response:
        """``POST /tickets/{id}/tags.json`` — REPLACES, despite the name.

        Modelled although no adapter path calls it, because the measurement is
        counter-intuitive enough to be a live trap: on 2026-08-17 a POST of one
        tag reduced ticket 3's tags from
        ``['ai-processed', 'cxforge-verify', 'probe-tags-endpoint']`` to
        ``['probe-post-tags']``, wiping both the loop guard and the owner's
        marker. Anyone who "simplifies" ``_merge_tags`` to a POST must fail a
        test here, not in production.
        """
        self.tickets[ticket_id]["tags"] = list(json.loads(request.content)["tags"])
        return httpx.Response(201, json={"tags": list(self.tickets[ticket_id]["tags"])})

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
            # Authored by whoever the token acts as — `authenticated_user_id`,
            # NOT the configured `ai_user_id`. Hardcoding AI_USER_ID here was
            # the second half of this fake's dishonesty: it made "the comment
            # author equals ZENDESK_AI_USER_ID" true by construction, which is
            # precisely the production assumption that turned out false.
            author_id = int(self.authenticated_user_id)
            self.comments[ticket_id].append(
                {
                    "id": comment_id,
                    "author_id": author_id,
                    "body": comment.get("body", comment.get("html_body", "")),
                    "html_body": comment.get("html_body", comment.get("body", "")),
                    "public": is_public,
                    "created_at": self._next_timestamp(),
                }
            )
            # Real Zendesk names the actor on the Comment event and on the
            # enclosing audit; both are reproduced because
            # `ZendeskAdapter._check_comment_author` reads the event first and
            # falls back to the audit.
            comment_event = {
                "type": "Comment",
                "id": comment_id,
                "public": is_public,
                "author_id": author_id,
            }

        # MEASURED, not assumed. On `PUT /tickets/{id}.json` real Zendesk
        # REPLACES the whole tag set from `tags`, and does not have an
        # `additional_tags` field at all — it belongs to `update_many`, and an
        # unknown key in the `ticket` object is discarded with a 200 exactly
        # like the `banana_tags` control field was. So this handler applies
        # `tags` destructively and IGNORES `additional_tags` entirely.
        #
        # Not ignoring it is what made this fake lie: the adapter's loop-guard
        # tag rode in that field, the fake honoured it, and every tag assertion
        # in the suite passed against a ticket Zendesk would have left untagged.
        if "tags" in patch:
            ticket["tags"] = list(patch["tags"])
        if "status" in patch:
            ticket["status"] = patch["status"]
        if "group_id" in patch:
            ticket["group_id"] = patch["group_id"]

        events = [comment_event] if comment_event else []
        audit = {
            "id": next(self._audit_ids),
            "author_id": int(self.authenticated_user_id),
            "events": events,
        }
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
