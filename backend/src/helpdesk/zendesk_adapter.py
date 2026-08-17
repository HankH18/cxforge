"""ZendeskAdapter — the only place Zendesk's quirks live (DESIGN §HelpdeskPort).

Quirks handled here, and nowhere else in the codebase:

- OAuth 2.0 bearer auth only. Zendesk API tokens are on a staged removal
  schedule (DESIGN "Decisions & rationale") — never implement or accept one,
  even for a spike. Re-verified 2026-08-17 against Zendesk's published
  schedule: phase 1 landed 2026-07-28, phase 2 blocks new token creation
  2026-10-27, phase 3 deactivates every remaining token 2027-04-30, and
  "accounts created on and after July 28, 2026, cannot create or use API
  tokens" — which this account (admin user created 2026-08-14) is. So the
  simpler-looking Basic-auth alternative is not merely forbidden by SPEC
  ("OAuth 2.0 only (API tokens are being deprecated — never use them)"), it
  is unavailable to this account.
- **The access token expires in 30 minutes.** It is a JWT carrying one
  ``exp`` claim, and renewing it is ``ZendeskCredentials``' job, not this
  class's — but note the consequence for every method below: the bearer
  token is resolved per request (``_auth_header``), never captured once at
  construction, because a refresh mid-run changes it.
- One comment per ticket update: a public reply and an internal note are
  necessarily two separate PUTs (``_update_ticket`` calls), never merged.
- **A ticket update cannot write tags additively at all.** Measured against
  the live account on 2026-08-17 (see ``_merge_tags``): on
  ``PUT /tickets/{id}.json`` the ``tags`` field *replaces* the whole set, and
  ``additional_tags`` — the field this adapter used until now — is **silently
  discarded**, 200 OK, no error, no change. It is an ``update_many`` field,
  not part of the single-ticket schema, and unknown keys in the ``ticket``
  object are dropped without complaint. So the only additive write is the
  dedicated tags endpoint, and ``_merge_tags`` is the single place that
  speaks it. ``_update_ticket`` refuses a patch that mentions either tag
  field, and calls ``_ensure_loop_guard_tag`` *before* the update so the
  ``ai-processed`` guard is already on the ticket when the write that could
  fire the trigger lands.
- 429 (honoring ``Retry-After``) and 5xx are retried with backoff; other 4xx
  responses are not retryable and surface as ``HelpdeskAPIError`` immediately.
- Comment author identity only tells you a Zendesk user id; mapping that to
  the normalized ``author_kind`` (customer / agent / ai) happens only here.
"""

from __future__ import annotations

import logging
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
    HelpdeskAuthError,
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
from helpdesk.zendesk_credentials import ZendeskCredentials

# The loop-guard tag: the Zendesk trigger that fires the webhook carries the
# nullifying condition "tags not include ai-processed" (see the T-4 runbook).
# Every write this adapter makes MUST carry it, or the trigger re-fires on
# the agent's own update and the webhook loops forever. Funnelling every
# write through _update_ticket (below) is what makes forgetting it
# structurally impossible rather than a per-method discipline problem.
#
# Funnelling was necessary and — until 2026-08-17 — not sufficient: the funnel
# wrote the tag into a field Zendesk ignores, so a complete successful run left
# ticket 3 tagged ['cxforge-verify'] with no ai-processed anywhere. See
# _merge_tags for the measurement.
AI_PROCESSED_TAG = "ai-processed"

_MAX_BACKOFF_SECONDS = 30.0

logger = logging.getLogger(__name__)


class _Unauthorized(Exception):
    """Internal signal: Zendesk answered 401. Never escapes this module.

    Deliberately NOT a ``RetryableResponse`` — that type is what the tenacity
    loop retries, and retrying a 401 with the same dead token is exactly the
    masking behaviour this must not do. It is caught in ``_request``, which
    converts it into one refresh attempt or a ``HelpdeskAuthError``.
    """

    def __init__(self, body: str) -> None:
        super().__init__(f"unauthorized: {body[:200]}")
        self.body = body


class ZendeskAdapter:
    """HelpdeskPort implementation backed by the Zendesk Support REST API."""

    def __init__(
        self,
        *,
        subdomain: str | None = None,
        oauth_token: str | None = None,
        ai_user_id: str | None = None,
        client: httpx.Client | None = None,
        credentials: ZendeskCredentials | None = None,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build an adapter. Every credential defaults to its env var (see
        .env.example) so production code never has to thread them through;
        tests inject them (and a fake ``sleep``) directly.

        ``credentials`` is the renewal seam. Left unset, one is built from the
        environment, which is what every production call site does
        (``worker.main``, ``portal.deps``, ``scripts/live_smoke.py``) — so the
        30-minute access-token expiry is handled without any of them changing.
        ``oauth_token`` still works and still wins, for tests and for a
        one-shot script holding a token it was handed.
        """
        subdomain = subdomain or os.environ.get("ZENDESK_SUBDOMAIN")
        if not subdomain:
            raise HelpdeskConfigError(
                "ZENDESK_SUBDOMAIN is required to build a ZendeskAdapter."
            )
        if credentials is None:
            credentials = ZendeskCredentials.from_env(
                subdomain=subdomain, access_token=oauth_token
            )
        self._credentials = credentials
        self._ai_user_id: str | None
        if ai_user_id is not None:
            self._ai_user_id = ai_user_id
        else:
            self._ai_user_id = os.environ.get("ZENDESK_AI_USER_ID")
        # NOTE: no Authorization header here. It is applied per request from
        # `self._credentials`, because a refresh mid-run replaces the token and
        # a header baked in at construction would pin every later request to
        # the dead one.
        self._client = client or httpx.Client(
            base_url=f"https://{subdomain}.zendesk.com/api/v2",
            timeout=10.0,
        )
        self._max_attempts = max_attempts
        self._sleep = sleep
        # Tickets this instance has already put `ai-processed` on. See
        # _ensure_loop_guard_tag: correctness does not depend on this cache,
        # only request count does.
        self._loop_guard_tagged: set[str] = set()
        # Resolved lazily by authenticated_user_id(), which costs one request.
        self._resolved_user_id: str | None = None

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
        self._check_comment_author(ticket_id, response)
        return self._message_ref(ticket_id, response, public=True)

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        # A SEPARATE PUT from post_public_reply: Zendesk accepts only one
        # comment per ticket update, so a public reply and an internal note
        # can never be combined into a single request.
        response = self._update_ticket(
            ticket_id, {"comment": {"body": body, "public": False}}
        )
        self._check_comment_author(ticket_id, response)
        return self._message_ref(ticket_id, response, public=False)

    def authenticated_user_id(self) -> str:
        """The Zendesk user id this adapter's OAuth token actually acts as.

        This — not ``ZENDESK_AI_USER_ID`` — is the id that appears as
        ``author_id`` on every comment the agent writes, and therefore the id
        the ingress self-event guard has to compare against. Costs one request
        the first time and is cached: the token's identity cannot change
        without a new token, and a refresh preserves it.
        """
        if self._resolved_user_id is None:
            payload = self._request("GET", "/users/me.json").json()
            self._resolved_user_id = str(payload["user"]["id"])
        return self._resolved_user_id

    def verify_ai_user_id(self) -> str:
        """Fail loudly if ``ZENDESK_AI_USER_ID`` is not who the token is.

        Exists because the configured id and the token's real identity are two
        independent facts that nothing compared, and on 2026-08-17 they
        disagreed in production: ``ZENDESK_AI_USER_ID`` named the dedicated
        "Othram AI Agent" user (54404962250395) while the token acted as the
        owner's admin account (54402664002843). Ingress's self-event guard
        compares ``comment_author_id`` against the configured value, so it was
        comparing against an id that appears in no event this system will ever
        receive — a guard that cannot fire, with nothing red anywhere.

        Returns the authenticated id. Raises ``HelpdeskConfigError`` on a
        definite mismatch; a *missing* value is a warning, not a failure,
        because ``.env.example`` ships it blank and the guard degrades to
        "off" rather than to "wrong".
        """
        actual = self.authenticated_user_id()
        if not self._ai_user_id:
            logger.warning(
                "ZENDESK_AI_USER_ID is not set, so ingress's self-event loop guard "
                "is disabled. This token acts as user %s — set that.",
                actual,
            )
            return actual
        if str(self._ai_user_id) != actual:
            logger.error(
                "ZENDESK_AI_USER_ID (%s) is NOT the user this OAuth token acts as "
                "(%s). Every comment the agent writes will be authored by %s, so "
                "ingress's self-event guard can never fire and the agent's own "
                "replies will be processed as customer events.",
                self._ai_user_id,
                actual,
                actual,
            )
            raise HelpdeskConfigError(
                f"ZENDESK_AI_USER_ID is {self._ai_user_id!r} but this OAuth token "
                f"acts as user {actual!r}. The ingress self-event loop guard "
                f"compares against the configured value and would never match. "
                f"Set ZENDESK_AI_USER_ID={actual}, or re-authorize the token as "
                f"the configured user."
            )
        return actual

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        # NOT a ticket update: see _merge_tags. A ticket update physically
        # cannot add a tag without replacing the whole set.
        self._merge_tags(ticket_id, list(tags))

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        self._update_ticket(ticket_id, {"status": status})

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        self._update_ticket(ticket_id, {"group_id": int(group.group_id)})

    # -- internals --------------------------------------------------------

    def _merge_tags(self, ticket_id: str, tags: list[str]) -> None:
        """The ONLY additive tag write, and the ONLY place the loop-guard tag
        actually reaches Zendesk.

        MEASURED against the live account on 2026-08-17, from a ticket whose
        tags were ``['cxforge-verify']``. All four calls answered 2xx:

        =====================================================  ==================
        request                                                resulting tags
        =====================================================  ==================
        ``PUT  /tickets/3.json  {"additional_tags":[...]}``     unchanged (!)
        ``PUT  /tickets/3.json  {"tags":["replace-probe"]}``    replaced
        ``PUT  /tickets/3/tags.json  {"tags":["ai-processed"]}``  MERGED
        ``POST /tickets/3/tags.json  {"tags":["x"]}``           replaced (!)
        =====================================================  ==================

        Two traps in that table, both counter-intuitive, both load-bearing:

        1. ``additional_tags`` is not merely wrong here, it is *inert* — a
           control request carrying a field Zendesk has never heard of
           (``banana_tags``) behaved identically, 200 with no effect. Unknown
           keys in the ``ticket`` object are discarded silently. That is why
           the old code's three 200-OK PUTs "proved" nothing.
        2. ``PUT`` on the tags sub-resource is the *additive* one and ``POST``
           is the *destructive* one, which is the reverse of what the method
           names suggest and the reverse of the reading most people take from
           Zendesk's "Add Tags" / "Set Tags" labels. Do not "simplify" this to
           a POST: it wiped ``cxforge-verify`` and ``ai-processed`` off the
           live ticket during the probe.
        """
        requested = list(tags)
        if AI_PROCESSED_TAG not in requested:
            requested.append(AI_PROCESSED_TAG)
        self._request("PUT", f"/tickets/{ticket_id}/tags.json", json_body={"tags": requested})
        self._loop_guard_tagged.add(ticket_id)

    def _ensure_loop_guard_tag(self, ticket_id: str) -> None:
        """Put ``ai-processed`` on the ticket BEFORE a write that can fire the
        trigger.

        Ordering is the whole point and it is not interchangeable. Zendesk
        evaluates a trigger's conditions against the ticket state produced by
        the update, so a public reply posted *before* the tag exists satisfies
        the trigger ("Comment is Public") with the nullifying condition ("Tags
        contains none of ai-processed") not yet met — the webhook fires on the
        agent's own comment. Tagging afterwards is too late for the very reply
        that needed guarding. Tagging first means the post-update state already
        carries the tag and the trigger is nullified.

        A failure here therefore aborts the write instead of proceeding
        unguarded: an unguarded public reply is how a loop starts, and a loop
        spams a real customer and bills real Anthropic tokens until someone
        notices. Failing the run is the cheaper mistake.

        Cached per ticket per adapter instance so a run's later writes cost no
        extra request — populated only on a *successful* merge, so a failed tag
        write is retried by the next write rather than assumed done.
        """
        if ticket_id in self._loop_guard_tagged:
            return
        self._merge_tags(ticket_id, [AI_PROCESSED_TAG])

    def _update_ticket(self, ticket_id: str, ticket_patch: dict[str, Any]) -> httpx.Response:
        """The ONLY path to ``PUT /tickets/{id}.json``.

        Every write operation above funnels through here, and the loop-guard
        tag is applied through ``_ensure_loop_guard_tag`` before the update
        goes out — there is no way to construct a write that skips it.

        Tags are refused outright rather than forwarded. ``tags`` would replace
        the ticket's whole set (wiping ``ai-processed`` itself), and
        ``additional_tags`` is silently discarded — a no-op that looks like a
        success, which is exactly the failure this method shipped with for the
        project's whole life. Neither belongs in a ticket patch, so mentioning
        either is a programming error and is raised as one.
        """
        for field in ("tags", "additional_tags"):
            if field in ticket_patch:
                raise HelpdeskConfigError(
                    f"a ticket update must not carry {field!r}: on "
                    f"PUT /tickets/{{id}}.json 'tags' replaces the entire tag set "
                    "and 'additional_tags' is silently ignored. Use _merge_tags "
                    "(PUT /tickets/{id}/tags.json), the only additive tag write."
                )
        self._ensure_loop_guard_tag(ticket_id)
        return self._request(
            "PUT", f"/tickets/{ticket_id}.json", json_body={"ticket": dict(ticket_patch)}
        )

    def _check_comment_author(self, ticket_id: str, response: httpx.Response) -> None:
        """Read the just-written comment's author back off the write response.

        The strongest available detector for the ``ZENDESK_AI_USER_ID``
        mismatch class, and it costs **nothing**: the audit Zendesk returns
        from the ticket update already names the author of the comment it just
        created, so this compares configuration against the effect the system
        actually produced rather than against another piece of configuration.
        Had this existed, the first reply the agent ever posted would have said
        so — instead it took reading a live ticket by hand an hour after the
        fact.

        Deliberately does **not** raise. The comment is already posted and
        visible to the customer; raising here would send
        ``worker.main.run_ticket`` down its failure path, release the dedup row
        (ADR-003) and make the *next* delivery of the same event post a second
        reply. A wrong id degrades one loop-guard line; a duplicate customer
        reply is worse. ``verify_ai_user_id`` is the raising form, for a
        preflight where nothing has been written yet.
        """
        if not self._ai_user_id:
            return
        audit = response.json().get("audit", {})
        events = audit.get("events", [])
        comment = next((event for event in events if event.get("type") == "Comment"), None)
        if comment is None:
            return
        # Both places are read because the audit-level `author_id` is the one
        # Zendesk documents as always present on a ticket audit, while the
        # per-event one is what a Comment event carries; for a ticket update
        # they are necessarily the same actor. Falling back rather than picking
        # one means this cannot go silently blind if either field moves.
        author_id = comment.get("author_id") or audit.get("author_id")
        if author_id is None or str(author_id) == str(self._ai_user_id):
            return
        logger.error(
            "the comment just written to ticket %s was authored by Zendesk user %s, "
            "not the configured ZENDESK_AI_USER_ID %s. Ingress's self-event loop "
            "guard compares against the configured value, so it will NOT drop this "
            "comment's webhook and the agent may process its own reply. Set "
            "ZENDESK_AI_USER_ID=%s (docs/zendesk-runbook.md step 3).",
            ticket_id,
            author_id,
            self._ai_user_id,
            author_id,
        )

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
        """Send one API call, renewing the credential at most once.

        401 handling is deliberately **not** part of the tenacity retry set
        below. A 401 is not transient: retrying it with the same dead token
        just burns attempts and buries the cause, and this stack cannot afford
        a masked credential failure — ``worker/main.py`` books a swallowed
        exception as an arq success, so the ERROR log is the only signal that
        disagrees. So it gets exactly one refresh and exactly one retry, and
        anything still 401 after that raises ``HelpdeskAuthError``.
        """
        try:
            return self._send(method, path, params, json_body)
        except _Unauthorized:
            logger.warning(
                "Zendesk answered 401 for %s %s; attempting a single token "
                "refresh before one retry",
                method,
                path,
            )
            # Raises HelpdeskAuthError (after an ERROR log) if the credential
            # cannot be renewed — the permanently-dead case, which must escape
            # rather than be retried.
            self._credentials.refresh()
            try:
                return self._send(method, path, params, json_body)
            except _Unauthorized as second:
                logger.error(
                    "Zendesk still answered 401 for %s %s after a successful "
                    "token refresh. The credential is not the whole problem — "
                    "check the OAuth client's scopes and the AI user's role "
                    "(docs/OWNER-ACTIONS.md OA-4). Response: %s",
                    method,
                    path,
                    second.body[:300],
                )
                raise HelpdeskAuthError(
                    401,
                    f"still unauthorized after a token refresh: {second.body[:300]}",
                ) from None

    def _send(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        json_body: dict[str, Any] | None,
    ) -> httpx.Response:
        """One request plus the transient-failure retry loop (429 / 5xx)."""

        def attempt() -> httpx.Response:
            # Resolved per attempt, not per _request: a refresh that happened
            # between attempts must take effect on the next one.
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {self._credentials.bearer()}"},
            )
            if response.status_code == 401:
                raise _Unauthorized(response.text)
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
