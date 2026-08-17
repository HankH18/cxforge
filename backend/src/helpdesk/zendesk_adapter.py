"""ZendeskAdapter — the only place Zendesk's quirks live (DESIGN §HelpdeskPort).

Quirks handled here, and nowhere else in the codebase:

- OAuth 2.0 bearer auth only. Zendesk API tokens are on a staged removal
  schedule (DESIGN "Decisions & rationale") — never implement or accept one,
  even for a spike.
- One comment per ticket update: a public reply and an internal note are
  necessarily two separate PUTs (``_update_ticket`` calls), never merged.
- Tag writes must be additive (Zendesk's ``tags`` field on a ticket update
  *replaces* the set; ``additional_tags`` is the additive field) — every
  write funnels through ``_update_ticket``, which uses ``additional_tags``
  and folds in the ``ai-processed`` loop-guard tag unconditionally.
- 429 (honoring ``Retry-After``) and 5xx are retried with backoff; other 4xx
  responses are not retryable and surface as ``HelpdeskAPIError`` immediately.
- Comment author identity only tells you a Zendesk user id; mapping that to
  the normalized ``author_kind`` (customer / agent / ai) happens only here.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
)

from helpdesk.errors import (
    HelpdeskAPIError,
    HelpdeskConfigError,
    RateLimited,
    ServerUnavailable,
)
from helpdesk.models import (
    AuthorKind,
    EscalationGroup,
    Message,
    MessageRef,
    Ticket,
    TicketStatus,
    TicketSummary,
)

# The loop-guard tag: the Zendesk trigger that fires the webhook carries the
# nullifying condition "tags not include ai-processed" (see the T-4 runbook).
# Every write this adapter makes MUST carry it, or the trigger re-fires on
# the agent's own update and the webhook loops forever. Funnelling every
# write through _update_ticket (below) is what makes forgetting it
# structurally impossible rather than a per-method discipline problem.
AI_PROCESSED_TAG = "ai-processed"

_MAX_BACKOFF_SECONDS = 30.0


class ZendeskAdapter:
    """HelpdeskPort implementation backed by the Zendesk Support REST API."""

    def __init__(
        self,
        *,
        subdomain: str | None = None,
        oauth_token: str | None = None,
        ai_user_id: str | None = None,
        client: httpx.Client | None = None,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build an adapter. Every credential defaults to its env var (see
        .env.example) so production code never has to thread them through;
        tests inject them (and a fake ``sleep``) directly.
        """
        subdomain = subdomain or os.environ.get("ZENDESK_SUBDOMAIN")
        oauth_token = oauth_token or os.environ.get("ZENDESK_OAUTH_TOKEN")
        if not subdomain or not oauth_token:
            raise HelpdeskConfigError(
                "ZENDESK_SUBDOMAIN and ZENDESK_OAUTH_TOKEN (OAuth 2.0 bearer "
                "token — never an API token) are required to build a "
                "ZendeskAdapter."
            )
        self._ai_user_id: str | None
        if ai_user_id is not None:
            self._ai_user_id = ai_user_id
        else:
            self._ai_user_id = os.environ.get("ZENDESK_AI_USER_ID")
        self._client = client or httpx.Client(
            base_url=f"https://{subdomain}.zendesk.com/api/v2",
            headers={"Authorization": f"Bearer {oauth_token}"},
            timeout=10.0,
        )
        self._max_attempts = max_attempts
        self._sleep = sleep

    # -- HelpdeskPort ---------------------------------------------------

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        response = self._request(
            "GET", f"/tickets/{ticket_id}.json", params={"include": "users"}
        )
        payload = response.json()
        ticket = payload["ticket"]
        users = {user["id"]: user for user in payload.get("users", [])}
        requester = users.get(ticket.get("requester_id"), {})
        return Ticket(
            id=str(ticket["id"]),
            subject=ticket["subject"],
            requester_email=requester.get("email", ""),
            status=ticket["status"],
            tags=list(ticket.get("tags", [])),
            created_at=ticket["created_at"],
        )

    def fetch_conversation(self, ticket_id: str) -> list[Message]:
        response = self._request(
            "GET", f"/tickets/{ticket_id}/comments.json", params={"include": "users"}
        )
        payload = response.json()
        users = {user["id"]: user for user in payload.get("users", [])}
        messages = [
            Message(
                id=str(comment["id"]),
                author_kind=self._author_kind(comment["author_id"], users),
                text=comment.get("plain_body") or comment.get("body", ""),
                public=comment["public"],
                created_at=comment["created_at"],
            )
            for comment in payload.get("comments", [])
        ]
        # Zendesk's default sort is already chronological, but the port's
        # contract ("chronological order") shouldn't depend on that holding
        # for every query variant (pagination, future sort params) — sort
        # explicitly rather than trusting API ordering.
        messages.sort(key=lambda message: message.created_at)
        return messages

    def fetch_requester_history(
        self, requester_email: str, *, exclude_ticket_id: str, limit: int = 5
    ) -> list[TicketSummary]:
        """The requester's other tickets, newest first (ADR-009).

        Zendesk quirks handled here and nowhere else, as with everything in
        this class:

        - There is no "tickets by requester email" endpoint. The Search API
          is the supported route, and its ``requester:`` term takes an email
          address. ``type:ticket`` is required or the same query also returns
          users and organizations, which have no ``subject`` at all.
        - Sorting is a query parameter, not part of the query string;
          ``sort_by``/``sort_order`` are what Search honours.
        - The current ticket comes back in its own requester's results, so
          ``exclude_ticket_id`` is filtered client-side *after* the fetch,
          and ``per_page`` asks for one extra row so excluding it cannot
          silently shorten the list below ``limit``.
        - A malformed/oversized search query answers 422, which
          ``_request`` already surfaces as a typed ``HelpdeskAPIError``.
        """
        response = self._request(
            "GET",
            "/search.json",
            params={
                "query": f"type:ticket requester:{requester_email}",
                "sort_by": "created_at",
                "sort_order": "desc",
                "per_page": str(limit + 1),
            },
        )
        results = response.json().get("results", [])
        summaries: list[TicketSummary] = []
        for row in results:
            ticket_id = str(row["id"])
            if ticket_id == exclude_ticket_id:
                continue
            summaries.append(
                TicketSummary(
                    id=ticket_id,
                    subject=row.get("subject") or "",
                    status=row["status"],
                    created_at=row["created_at"],
                    tags=list(row.get("tags", [])),
                )
            )
            if len(summaries) == limit:
                break
        return summaries

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef:
        response = self._update_ticket(
            ticket_id, {"comment": {"html_body": html_body, "public": True}}
        )
        return self._message_ref(ticket_id, response, public=True)

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        # A SEPARATE PUT from post_public_reply: Zendesk accepts only one
        # comment per ticket update, so a public reply and an internal note
        # can never be combined into a single request.
        response = self._update_ticket(
            ticket_id, {"comment": {"body": body, "public": False}}
        )
        return self._message_ref(ticket_id, response, public=False)

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        self._update_ticket(ticket_id, {"additional_tags": list(tags)})

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        self._update_ticket(ticket_id, {"status": status})

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        self._update_ticket(ticket_id, {"group_id": int(group.group_id)})

    # -- internals --------------------------------------------------------

    def _update_ticket(self, ticket_id: str, ticket_patch: dict[str, Any]) -> httpx.Response:
        """The ONLY path to ``PUT /tickets/{id}.json``.

        Every write operation above funnels through here, and this method
        unconditionally folds ``ai-processed`` into ``additional_tags`` —
        there is no way to construct a write that skips it. Using
        ``additional_tags`` rather than ``tags`` is what keeps ``add_tags``
        (and this loop-guard tag) additive instead of clobbering whatever
        tags the ticket already carried.
        """
        patch = dict(ticket_patch)
        requested_tags = list(patch.get("additional_tags", []))
        if AI_PROCESSED_TAG not in requested_tags:
            requested_tags.append(AI_PROCESSED_TAG)
        patch["additional_tags"] = requested_tags
        return self._request("PUT", f"/tickets/{ticket_id}.json", json_body={"ticket": patch})

    def _message_ref(self, ticket_id: str, response: httpx.Response, *, public: bool) -> MessageRef:
        payload = response.json()
        audit = payload.get("audit", {})
        events = audit.get("events", [])
        comment_event = next((event for event in events if event.get("type") == "Comment"), None)
        message_id = str(comment_event["id"]) if comment_event else str(audit.get("id", ""))
        return MessageRef(ticket_id=ticket_id, message_id=message_id, public=public)

    def _author_kind(self, author_id: int, users: dict[int, dict[str, Any]]) -> AuthorKind:
        if self._ai_user_id is not None and str(author_id) == str(self._ai_user_id):
            return "ai"
        if users.get(author_id, {}).get("role") == "end-user":
            return "customer"
        return "agent"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        def attempt() -> httpx.Response:
            response = self._client.request(method, path, params=params, json=json_body)
            if response.status_code == 429:
                raise RateLimited(_parse_retry_after(response))
            if response.status_code >= 500:
                raise ServerUnavailable(response.status_code)
            if response.status_code >= 400:
                raise HelpdeskAPIError(response.status_code, response.text)
            return response

        retrying = Retrying(
            retry=retry_if_exception_type((RateLimited, ServerUnavailable)),
            wait=self._wait,
            stop=stop_after_attempt(self._max_attempts),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            return retrying(attempt)
        except RateLimited:
            raise HelpdeskAPIError(429, "rate limited: retries exhausted") from None
        except ServerUnavailable as exc:
            raise HelpdeskAPIError(exc.status_code, "server error: retries exhausted") from None
        except RetryError as exc:  # pragma: no cover - reraise=True makes this unreachable
            raise HelpdeskAPIError(0, f"retries exhausted: {exc}") from None

    @staticmethod
    def _wait(retry_state: RetryCallState) -> float:
        """429 waits exactly what Zendesk's ``Retry-After`` said; a 5xx backs
        off exponentially (capped) since the provider gave no explicit hint.
        """
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, RateLimited):
            return exc.retry_after
        attempt_number = retry_state.attempt_number
        return min(float(2 ** (attempt_number - 1)), _MAX_BACKOFF_SECONDS)


def _parse_retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header is None:
        return 1.0
    try:
        return float(header)
    except ValueError:
        return 1.0
